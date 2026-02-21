#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MENU OLED + ENCODEUR (NUXI)
1) Mode "Poids (balance)" : lecture HX711 + affichage OLED + terminal
2) Mode "Distribution manuelle" : ton menu existant + POST /distribute

Contrôle :
- Tourner encodeur : naviguer
- Appui court : valider
- Appui LONG (>= 1.5s) : retour au menu principal (dans n'importe quel écran)
"""

import time
import datetime
import statistics as stat
import json

import RPi.GPIO as GPIO
import board
import busio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306
import requests
from supabase import create_client, Client

import threading

# =========================================================
# ====================== CONFIG OLED ======================
# =========================================================
WIDTH = 128
HEIGHT = 64
I2C_ADDRESS = 0x3C  # mets 0x3D si besoin

# =========================================================
# ===================== CONFIG ENCODEUR ===================
# =========================================================
PIN_CLK = 17
PIN_DT  = 27
PIN_SW  = 22

POLL_DELAY_S = 0.001
ROT_DEBOUNCE_S = 0.0008
BTN_DEBOUNCE_S = 0.15
STEPS_PER_CLICK = 4
LONG_PRESS_S = 1.5

# Quadrature decoding table (Gray code)
QUAD_TABLE = {
    0b0001: +1,
    0b0010: -1,
    0b0100: -1,
    0b0111: +1,
    0b1000: +1,
    0b1011: -1,
    0b1101: -1,
    0b1110: +1,
}

# =========================================================
# ====================== CONFIG HX711 =====================
# =========================================================
DT_PIN = 23  # ORANGE
SCK_PIN = 24 # BLANC

SCALE_RATIO = 389.38567  # <-- MODIFIE ICI (unités HX711 par gramme)
READINGS = 2
PERIOD_S = 0.003

OLED_W = 128
OLED_H = 64
OLED_ADDR = I2C_ADDRESS

# Détection "gamelle non finie" (gardé tel quel)
STABLE_TIME_S = 5                 # (tu avais mis 5s ici)
STABLE_TOL_G = 1.0
MIN_REMAINING_G = 5.0
APP_TRIGGER_COOLDOWN_S = 30 * 60

# =========================
# CONFIG SUPABASE
# =========================
SUPABASE_URL = "https://ulznwgmstojtkroyggmb.supabase.co" 
SUPABASE_KEY = "XXXXX"
DB_LOG_INTERVAL = 30  # (en secondes)
TARGET_USER_ID = "4201037f-d85c-4467-b47c-0d276993e683"

# =========================
# GLOBALES (THREADING)
# =========================
CURRENT_WEIGHT = 0.0  # Variable partagée
WEIGHT_LOCK = threading.Lock()

def trigger_app_leftover_alert(remaining_g: float):
    print(f"[ALERTE APP] Le chat n'a pas fini sa gamelle. Reste ~{remaining_g:.1f} g.")
    # TODO: envoyer le trigger à l'application


def background_worker(hx, supabase):
    """
    Tâche de fond qui lit le HX711 en boucle et met à jour Supabase.
    """
    global CURRENT_WEIGHT
    
    print("[THREAD] Démarrage de la surveillance poids en arrière-plan.")
    last_db_log = 0
    
    # Variables pour détection stabilité (alerte app)
    stable_ref_g = None
    stable_since = None
    last_app_trigger = 0.0

    while True:
        try:
            # 1. Lecture du capteur
            # On lit le RAW/NET pour le debug interne, mais on stocke surtout le weight
            # raw = hx.get_raw_data_mean(READINGS) # Optionnel si tu ne l'affiches plus
            w_g = hx.get_weight_mean(READINGS)

            if w_g is not False:
                # Mise à jour de la variable globale (protégée par Lock)
                with WEIGHT_LOCK:
                    CURRENT_WEIGHT = float(w_g)

                now = time.time()

                # 2. Logique SUPABASE (Toutes les X minutes)
                if (now - last_db_log) >= DB_LOG_INTERVAL:
                    if supabase:
                        try:
                            supabase.table('users_profile') \
                                .update({'current_food_weight': float(w_g)}) \
                                .eq('user_id', TARGET_USER_ID) \
                                .execute()
                            print(f"[BG-SUPABASE] Stock mis à jour : {w_g:.2f}g")
                            last_db_log = now
                        except Exception as e:
                            print(f"[BG-SUPABASE] Erreur: {e}")

                # 3. Logique ALERTE GAMELLE (Copiée de ton ancien code)
                if float(w_g) >= MIN_REMAINING_G:
                    if stable_ref_g is None:
                        stable_ref_g = float(w_g)
                        stable_since = now
                    else:
                        if abs(float(w_g) - stable_ref_g) <= STABLE_TOL_G:
                            if stable_since is not None and (now - stable_since) >= STABLE_TIME_S:
                                if (now - last_app_trigger) >= APP_TRIGGER_COOLDOWN_S:
                                    trigger_app_leftover_alert(float(w_g))
                                    last_app_trigger = now
                                    stable_ref_g = float(w_g)
                                    stable_since = now
                        else:
                            stable_ref_g = float(w_g)
                            stable_since = now
                else:
                    stable_ref_g = None
                    stable_since = None
            
            else:
                # En cas d'erreur de lecture
                pass

            time.sleep(PERIOD_S)

        except Exception as e:
            print(f"[THREAD ERROR] {e}")
            time.sleep(1)


class HX711:
    """
    HX711 represents chip for reading load cells.
    """

    def __init__(self,
                 dout_pin,
                 pd_sck_pin,
                 gain_channel_A=128,
                 select_channel='A'):

        if not isinstance(dout_pin, int) or not isinstance(pd_sck_pin, int):
            raise TypeError("dout_pin et pd_sck_pin doivent être des int")

        self._pd_sck = pd_sck_pin
        self._dout = dout_pin

        self._gain_channel_A = 0
        self._offset_A_128 = 0
        self._offset_A_64 = 0
        self._offset_B = 0
        self._last_raw_data_A_128 = 0
        self._last_raw_data_A_64 = 0
        self._last_raw_data_B = 0
        self._wanted_channel = ''
        self._current_channel = ''
        self._scale_ratio_A_128 = 1.0
        self._scale_ratio_A_64 = 1.0
        self._scale_ratio_B = 1.0
        self._debug_mode = False
        self._data_filter = self.outliers_filter  # default filter

        # IMPORTANT
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        GPIO.setup(self._pd_sck, GPIO.OUT)  # clock output
        GPIO.setup(self._dout, GPIO.IN)     # data input
        GPIO.output(self._pd_sck, False)

        self.select_channel(select_channel)
        self.set_gain_A(gain_channel_A)

    def select_channel(self, channel):
        channel = channel.capitalize()
        if channel == 'A':
            self._wanted_channel = 'A'
        elif channel == 'B':
            self._wanted_channel = 'B'
        else:
            raise ValueError('channel doit être "A" ou "B"')

        self._read()
        time.sleep(0.5)

    def set_gain_A(self, gain):
        if gain not in (128, 64):
            raise ValueError('gain A doit être 128 ou 64')
        self._gain_channel_A = gain
        self._read()
        time.sleep(0.5)

    def zero(self, readings=30):
        if readings <= 0 or readings >= 100:
            raise ValueError("readings doit être dans [1..99]")

        result = self.get_raw_data_mean(readings)
        if result is False:
            return True  # erreur

        if self._current_channel == 'A' and self._gain_channel_A == 128:
            self._offset_A_128 = result
            return False
        elif self._current_channel == 'A' and self._gain_channel_A == 64:
            self._offset_A_64 = result
            return False
        elif self._current_channel == 'B':
            self._offset_B = result
            return False

        return True

    def set_scale_ratio(self, scale_ratio, channel='', gain_A=0):
        channel = channel.capitalize()
        if channel == 'A' and gain_A == 128:
            self._scale_ratio_A_128 = float(scale_ratio)
        elif channel == 'A' and gain_A == 64:
            self._scale_ratio_A_64 = float(scale_ratio)
        elif channel == 'B':
            self._scale_ratio_B = float(scale_ratio)
        elif channel == '':
            if self._current_channel == 'A' and self._gain_channel_A == 128:
                self._scale_ratio_A_128 = float(scale_ratio)
            elif self._current_channel == 'A' and self._gain_channel_A == 64:
                self._scale_ratio_A_64 = float(scale_ratio)
            else:
                self._scale_ratio_B = float(scale_ratio)
        else:
            raise ValueError('channel doit être "A" ou "B" (ou vide)')

    def set_data_filter(self, data_filter):
        if callable(data_filter):
            self._data_filter = data_filter
        else:
            raise TypeError("data_filter doit être une fonction")

    def set_debug_mode(self, flag=False):
        self._debug_mode = bool(flag)

    def _save_last_raw_data(self, channel, gain_A, data):
        if channel == 'A' and gain_A == 128:
            self._last_raw_data_A_128 = data
        elif channel == 'A' and gain_A == 64:
            self._last_raw_data_A_64 = data
        elif channel == 'B':
            self._last_raw_data_B = data

    def _ready(self):
        return GPIO.input(self._dout) == 0

    def _set_channel_gain(self, num):
        for _ in range(num):
            start_counter = time.perf_counter()
            GPIO.output(self._pd_sck, True)
            GPIO.output(self._pd_sck, False)
            end_counter = time.perf_counter()
            if end_counter - start_counter >= 0.00006:
                if self._debug_mode:
                    print("Pulse trop long (risque power-down)")
                result = self.get_raw_data_mean(6)
                if result is False:
                    return False
        return True

    def _read(self):
        GPIO.output(self._pd_sck, False)

        ready_counter = 0
        while (not self._ready() and ready_counter <= 40):
            time.sleep(0.01)
            ready_counter += 1
            if ready_counter >= 40:
                if self._debug_mode:
                    print("_read() not ready")
                return False

        data_in = 0
        for _ in range(24):
            start_counter = time.perf_counter()
            GPIO.output(self._pd_sck, True)
            GPIO.output(self._pd_sck, False)
            end_counter = time.perf_counter()
            if end_counter - start_counter >= 0.00006:
                if self._debug_mode:
                    print("Pulse trop long pendant lecture")
                return False
            data_in = (data_in << 1) | GPIO.input(self._dout)

        if self._wanted_channel == 'A' and self._gain_channel_A == 128:
            if not self._set_channel_gain(1):
                return False
            self._current_channel = 'A'
            self._gain_channel_A = 128
        elif self._wanted_channel == 'A' and self._gain_channel_A == 64:
            if not self._set_channel_gain(3):
                return False
            self._current_channel = 'A'
            self._gain_channel_A = 64
        else:
            if not self._set_channel_gain(2):
                return False
            self._current_channel = 'B'

        if data_in == 0x7fffff or data_in == 0x800000:
            return False

        if data_in & 0x800000:
            signed_data = -((data_in ^ 0xffffff) + 1)
        else:
            signed_data = data_in

        return signed_data

    def get_raw_data_mean(self, readings=30):
        backup_channel = self._current_channel
        backup_gain = self._gain_channel_A

        data_list = []
        for _ in range(readings):
            data_list.append(self._read())

        if readings > 2 and self._data_filter:
            filtered = self._data_filter(data_list)
            if not filtered:
                return False
            mean_val = stat.mean(filtered)
        else:
            cleaned = [x for x in data_list if x not in (False, True)]
            if not cleaned:
                return False
            mean_val = stat.mean(cleaned)

        self._save_last_raw_data(backup_channel, backup_gain, int(mean_val))
        return int(mean_val)

    def get_data_mean(self, readings=30):
        result = self.get_raw_data_mean(readings)
        if result is False:
            return False

        if self._current_channel == 'A' and self._gain_channel_A == 128:
            return result - self._offset_A_128
        elif self._current_channel == 'A' and self._gain_channel_A == 64:
            return result - self._offset_A_64
        else:
            return result - self._offset_B

    def get_weight_mean(self, readings=30):
        result = self.get_raw_data_mean(readings)
        if result is False:
            return False

        if self._current_channel == 'A' and self._gain_channel_A == 128:
            return float((result - self._offset_A_128) / self._scale_ratio_A_128)
        elif self._current_channel == 'A' and self._gain_channel_A == 64:
            return float((result - self._offset_A_64) / self._scale_ratio_A_64)
        else:
            return float((result - self._offset_B) / self._scale_ratio_B)

    def outliers_filter(self, data_list, stdev_thresh=1.0):
        data = [num for num in data_list if (num not in (-1, False, True))]
        if not data:
            return []

        median = stat.median(data)
        dists = [abs(x - median) for x in data]
        if len(dists) < 2:
            return [median]

        stdev = stat.stdev(dists)
        if stdev == 0:
            return [median]

        ratios = [dist / stdev for dist in dists]
        return [data[i] for i in range(len(data)) if ratios[i] < stdev_thresh]


def oled_render_weight(oled, grams):
    image = Image.new("1", (OLED_W, OLED_H))
    draw = ImageDraw.Draw(image)
    font_small = ImageFont.load_default()
    draw.text((0, 0), "Poids:", font=font_small, fill=255)
    draw.text((0, 16), f"{int(grams)} g", font=font_small, fill=255)
    oled.image(image)
    oled.show()

# =========================================================
# =================== CONFIG API (manuel) =================
# =========================================================
API_URL = "http://192.168.137.54:5001/distribute"
HTTP_TIMEOUT_S = 3.5
SHOW_RESULT_S = 2.0
FULLSCREEN_MSG_S = 1.2

PET_ID_MAP = {"Chat": "chat", "Chien": "chien"}
MANUAL_SCHEDULE_ID = None

MENU_ANIMAL = ["Chat", "Chien"]
MENU_CAT = ["Distribution manuelle", "Retour"]
MENU_DOG = ["Distribution manuelle", "Retour"]
MENU_CAT_PORTIONS = ["10 g", "20 g", "30 g", "Retour"]
MENU_DOG_PORTIONS = ["100 g", "125 g", "150 g", "Retour"]

STATE_ANIMAL = "animal"
STATE_CAT_MENU = "cat_menu"
STATE_DOG_MENU = "dog_menu"
STATE_CAT_PORTIONS = "cat_portions"
STATE_DOG_PORTIONS = "dog_portions"

def get_menu_for_state(state):
    if state == STATE_ANIMAL:
        return MENU_ANIMAL
    if state == STATE_CAT_MENU:
        return MENU_CAT
    if state == STATE_DOG_MENU:
        return MENU_DOG
    if state == STATE_CAT_PORTIONS:
        return MENU_CAT_PORTIONS
    if state == STATE_DOG_PORTIONS:
        return MENU_DOG_PORTIONS
    return []

def parse_grams(choice_str: str):
    s = choice_str.strip().lower()
    if s == "retour":
        return None
    try:
        num = s.replace("g", "").strip()
        return int(num)
    except Exception:
        return None

def now_iso_paris():
    dt = datetime.datetime.now(datetime.datetime.now().astimezone().tzinfo)
    return dt.isoformat(timespec="seconds")

def post_distribute(pet_label: str, grams: int):
    payload = {
        "schedule_id": MANUAL_SCHEDULE_ID,
        "pet_id": PET_ID_MAP.get(pet_label, pet_label),
        "portion_weight": grams,
        "scheduled_at": now_iso_paris(),
    }

    print("🔼 [HTTP] POST", API_URL)
    print("🔼 [HTTP] Payload =", payload)

    try:
        r = requests.post(
            API_URL,
            json=payload,
            timeout=HTTP_TIMEOUT_S,
            headers={"Content-Type": "application/json"},
        )
        if 200 <= r.status_code < 300:
            txt = (r.text or "").strip()
            print(f"✅ [HTTP] {r.status_code} - {txt}")
            return True, "OK"
        else:
            txt = (r.text or "").strip()
            print(f"❌ [HTTP] {r.status_code} - {txt}")
            return False, f"HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        print("❌ [HTTP] Timeout")
        return False, "TIMEOUT"
    except requests.exceptions.RequestException as e:
        print(f"❌ [HTTP] Erreur reseau: {e}")
        return False, "RESEAU"
    except Exception as e:
        print(f"❌ [HTTP] Erreur: {e}")
        return False, "ERREUR"

def draw_menu(oled, image, draw, font, title, items, selected_idx):
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)
    draw.text((0, 0), title[:21], font=font, fill=255)

    visible_lines = 3
    start = 0
    if len(items) > visible_lines:
        if selected_idx <= 1:
            start = 0
        elif selected_idx >= len(items) - 2:
            start = len(items) - visible_lines
        else:
            start = selected_idx - 1

    for line in range(visible_lines):
        i = start + line
        if i >= len(items):
            break
        y = (line + 1) * 16
        prefix = ">" if i == selected_idx else " "
        draw.text((0, y), f"{prefix} {items[i]}", font=font, fill=255)

    oled.image(image)
    oled.show()

def draw_fullscreen_message(oled, image, draw, big_font, text1, text2=""):
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)

    bbox1 = draw.textbbox((0, 0), text1, font=big_font)
    w1 = bbox1[2] - bbox1[0]
    x1 = max(0, (WIDTH - w1) // 2)
    y1 = 10
    draw.text((x1, y1), text1, font=big_font, fill=255)

    if text2:
        bbox2 = draw.textbbox((0, 0), text2, font=big_font)
        w2 = bbox2[2] - bbox2[0]
        x2 = max(0, (WIDTH - w2) // 2)
        y2 = 34
        draw.text((x2, y2), text2, font=big_font, fill=255)

    oled.image(image)
    oled.show()

# =========================================================
# =============== OUTILS ENCODEUR (généraux) ===============
# =========================================================
def encoder_init():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_DT,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_SW,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

def encoder_read_state():
    clk = GPIO.input(PIN_CLK)
    dt  = GPIO.input(PIN_DT)
    return (clk << 1) | dt

def wait_button_release():
    while GPIO.input(PIN_SW) == 0:
        time.sleep(0.01)

def get_press_type():
    """
    Retourne "short", "long" ou None
    (appelé quand on détecte sw==0 + debounce OK)
    """
    t0 = time.time()
    # on attend soit relâchement, soit long press
    while GPIO.input(PIN_SW) == 0:
        if time.time() - t0 >= LONG_PRESS_S:
            wait_button_release()
            return "long"
        time.sleep(0.01)
    # relâché avant long press
    return "short"

# =========================================================
# =================== MODE 1 : HX711 ======================
# =========================================================
def run_mode_weight(oled):
    print("=== MODE ECRAN POIDS ===")
    print("Affichage seul (lecture en background).")
    print("Appui LONG pour revenir au menu.\n")

    last_btn_time = 0.0

    while True:
        # Gestion Bouton (Retour menu)
        now = time.time()
        if GPIO.input(PIN_SW) == 0 and (now - last_btn_time) >= BTN_DEBOUNCE_S:
            press = get_press_type()
            last_btn_time = now
            if press == "long":
                print("[MODE POIDS] Retour menu principal.")
                break

        # Récupération du poids depuis la variable globale
        val_to_show = 0.0
        with WEIGHT_LOCK:
            val_to_show = CURRENT_WEIGHT
        
        # Affichage Terminal (optionnel, pour debug)
        # print(f"Display: {val_to_show:.2f} g")

        # Affichage OLED
        oled_render_weight(oled, val_to_show)

        time.sleep(0.1) # Rafraichissement écran fluide

# =========================================================
# ============ MODE 2 : Distribution manuelle ==============
# =========================================================
def run_mode_manual(oled, image, draw, font, big_font):
    print("=== MODE DISTRIBUTION MANUELLE ===")
    print("Appui LONG pour revenir au menu principal.\n")

    clk = GPIO.input(PIN_CLK)
    dt = GPIO.input(PIN_DT)
    prev_state = (clk << 1) | dt

    step_accum = 0
    last_rot_time = 0.0
    last_btn_time = 0.0

    state = STATE_ANIMAL
    selected = 0
    current_pet = None

    def render_current():
        items = get_menu_for_state(state)
        if state == STATE_ANIMAL:
            title = "Type de l'animal :"
        elif state == STATE_CAT_MENU:
            title = "Chat :"
        elif state == STATE_DOG_MENU:
            title = "Chien :"
        elif state in (STATE_CAT_PORTIONS, STATE_DOG_PORTIONS):
            title = "Choix de la portion :"
        else:
            title = ""
        draw_menu(oled, image, draw, font, title, items, selected)

    render_current()
    print("Menu prêt. Tourne pour naviguer, appuie pour valider.")

    while True:
        now = time.time()

        # -------- Rotation --------
        clk = GPIO.input(PIN_CLK)
        dt = GPIO.input(PIN_DT)
        curr_state = (clk << 1) | dt

        if curr_state != prev_state:
            if (now - last_rot_time) >= ROT_DEBOUNCE_S:
                idx = ((prev_state << 2) | curr_state) & 0x0F
                delta = QUAD_TABLE.get(idx, 0)

                # Si sens inversé chez toi, change ici:
                delta = -delta

                if delta != 0:
                    step_accum += delta
                    if abs(step_accum) >= STEPS_PER_CLICK:
                        move = 1 if step_accum > 0 else -1
                        items = get_menu_for_state(state)
                        if items:
                            selected = (selected + move) % len(items)
                            render_current()
                        step_accum = 0

                last_rot_time = now

            prev_state = curr_state

        # -------- Bouton --------
        sw = GPIO.input(PIN_SW)
        if sw == 0 and (now - last_btn_time) >= BTN_DEBOUNCE_S:
            press = get_press_type()
            last_btn_time = now

            if press == "long":
                print("[MODE MANUEL] Retour menu principal.")
                return

            # press == short
            items = get_menu_for_state(state)
            choice = items[selected] if items else ""

            if state == STATE_ANIMAL:
                if choice == "Chat":
                    current_pet = "Chat"
                    state = STATE_CAT_MENU
                    selected = 0
                    render_current()
                elif choice == "Chien":
                    current_pet = "Chien"
                    state = STATE_DOG_MENU
                    selected = 0
                    render_current()

            elif state == STATE_CAT_MENU:
                if choice == "Distribution manuelle":
                    state = STATE_CAT_PORTIONS
                    selected = 0
                    render_current()
                elif choice == "Retour":
                    state = STATE_ANIMAL
                    selected = 0
                    render_current()

            elif state == STATE_DOG_MENU:
                if choice == "Distribution manuelle":
                    state = STATE_DOG_PORTIONS
                    selected = 0
                    render_current()
                elif choice == "Retour":
                    state = STATE_ANIMAL
                    selected = 0
                    render_current()

            elif state in (STATE_CAT_PORTIONS, STATE_DOG_PORTIONS):
                if choice == "Retour":
                    state = STATE_CAT_MENU if state == STATE_CAT_PORTIONS else STATE_DOG_MENU
                    selected = 0
                    render_current()
                else:
                    grams = parse_grams(choice)
                    if grams is None:
                        render_current()
                    else:
                        draw_fullscreen_message(
                            oled, image, draw, big_font,
                            "Distribution", f"{current_pet} {grams}g"
                        )
                        print(f"[{current_pet.upper()}] Distribution manuelle: {grams}g")
                        time.sleep(FULLSCREEN_MSG_S)

                        ok, msg = post_distribute(current_pet, grams)

                        #if ok:
                            #draw_fullscreen_message(oled, image, draw, big_font, "Distribution", "en Cours")
                        #else:
                            #short = msg[:10]
                            #draw_fullscreen_message(oled, image, draw, big_font, "ERREUR", short)

                        time.sleep(SHOW_RESULT_S)
                        render_current()

        time.sleep(POLL_DELAY_S)

# =========================================================
# ====================== MENU PRINCIPAL ===================
# =========================================================
def draw_main_menu(oled, image, draw, title_font, font, selected_idx):
    items = ["Poids", "Distribution manuelle"]
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)

    # Titre + gros
    title = 'NUXI'
    draw.text((0, 0), title[:21], font=title_font, fill=255)

    # Items
    for i, it in enumerate(items):
        y = 24 + i * 16
        prefix = ">" if i == selected_idx else " "
        draw.text((0, y), f"{prefix} {it}", font=font, fill=255)


    oled.image(image)
    oled.show()

def main():
    print("=== NUXI - Menu OLED + Encodeur (Background Thread) ===")

    # 1. Init Supabase
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Connexion Supabase OK.")
    except Exception as e:
        print(f"Erreur Supabase: {e}")
        supabase = None

    # 2. Init OLED
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        oled = adafruit_ssd1306.SSD1306_I2C(WIDTH, HEIGHT, i2c, addr=I2C_ADDRESS)
        oled.fill(0); oled.show()
    except Exception as e:
        print(f"Erreur OLED/I2C : {e}")
        return

    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    # Fonts
    try:
        big_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except Exception:
        big_font = ImageFont.load_default()

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        title_font = big_font

    # 3. Init HX711 & TARE (CRITIQUE : Faire ça AVANT le thread)
    print("Init HX711...")
    hx = HX711(dout_pin=DT_PIN, pd_sck_pin=SCK_PIN, gain_channel_A=128, select_channel='A')
    hx.set_debug_mode(False)
    hx.set_data_filter(hx.outliers_filter)
    
    print("Tare en cours (ne rien toucher)...")
    err = hx.zero(readings=30)
    if err:
        print("ERREUR TARE. Vérifier balance.")
    else:
        print("Tare OK.")
    
    hx.set_scale_ratio(SCALE_RATIO)

    # 4. Init Encodeur
    encoder_init()

    # =========================
    # 5. LANCEMENT DU THREAD
    # =========================
    # On passe 'hx' et 'supabase' au thread pour qu'il s'en serve en permanence
    bg_thread = threading.Thread(target=background_worker, args=(hx, supabase))
    bg_thread.daemon = True  # Le thread s'arrêtera quand le programme principal quittera
    bg_thread.start()
    print("Thread background lancé.")

    # 6. Boucle Menu Principal
    prev_state = encoder_read_state()
    step_accum = 0
    last_rot_time = 0.0
    last_btn_time = 0.0

    selected = 0  # 0=poids, 1=manuel

    def render():
        draw_main_menu(oled, image, draw, title_font, font, selected)

    render()
    print("Menu principal prêt. Tourne pour naviguer, appui court = OK, appui LONG = quitter.")

    try:
        while True:
            now = time.time()

            # Rotation
            curr_state = encoder_read_state()
            if curr_state != prev_state:
                if (now - last_rot_time) >= ROT_DEBOUNCE_S:
                    idx = ((prev_state << 2) | curr_state) & 0x0F
                    delta = QUAD_TABLE.get(idx, 0)
                    delta = -delta  # sens
                    if delta != 0:
                        step_accum += delta
                        if abs(step_accum) >= STEPS_PER_CLICK:
                            move = 1 if step_accum > 0 else -1
                            selected = (selected + move) % 2
                            render()
                            step_accum = 0
                    last_rot_time = now
                prev_state = curr_state

            # Bouton
            if GPIO.input(PIN_SW) == 0 and (now - last_btn_time) >= BTN_DEBOUNCE_S:
                press = get_press_type()
                last_btn_time = now

                if press == "long":
                    print("Quitter.")
                    break

                # short press -> entrer dans mode
                if selected == 0:
                    # On appelle run_mode_weight SANS arguments hx/supabase
                    # car elle lit maintenant la variable globale
                    run_mode_weight(oled)
                else:
                    run_mode_manual(oled, image, draw, font, big_font)

                # retour menu principal
                render()

            time.sleep(POLL_DELAY_S)

    except KeyboardInterrupt:
        print("\nArrêt du programme.")
    finally:
        try:
            oled.fill(0)
            oled.show()
        except Exception:
            pass
        GPIO.cleanup()

if __name__ == "__main__":
    main()
