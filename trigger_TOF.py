import time
import board
import busio
import adafruit_vl53l0x
import requests
import cv2          
import boto3        
import os           
import threading    
from datetime import datetime, timedelta
from supabase import create_client, Client, ClientOptions
import json 

# --- CONFIGURATION RESEAU ---
IP_PC = "10.5.26.241"
PORT_PC = 5000
PORT_LOCAL = 5001

URL_START_IA = f"http://{IP_PC}:{PORT_PC}/trigger-ai"
URL_STOP_IA  = f"http://{IP_PC}:{PORT_PC}/stop-ai"
URL_FEEDER_LOCAL = f"http://127.0.0.1:{PORT_LOCAL}/distribute"

# --- CONFIGURATION TOF ---
SEUIL_DETECTION_MM = 1000   
NB_VALIDATIONS_START = 5   
NB_VALIDATIONS_STOP = 30   

# --- CONFIGURATION TRAINING ---
AWS_ACCESS_KEY = "XXXX"
AWS_SECRET_KEY = "XXXXX"
BUCKET_NAME = "pfe-pets-dataset-storage"
RTSP_URL = "rtsp://127.0.0.1:8554/cam1" 

# --- CONFIGURATION SUPABASE ---
SUPABASE_URL = "https://ulznwgmstojtkroyggmb.supabase.co"
SUPABASE_KEY = "XXXXX"

try:
    options = ClientOptions(postgrest_client_timeout=5)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
except Exception as e:
    print(f"Warning Erreur init Supabase: {e}")
    supabase = None

try:
    s3 = boto3.client('s3', aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)
except Exception as e:
    print(f"Warning Erreur init S3: {e}")
    s3 = None

# --- FONCTIONS HELPERS ---

def log_detection(det_type, pet_id=None, details=None):
    if not supabase: return
    try:
        data = {
            "detection_type": det_type,
            "pet_id": pet_id,
            "details": details,
            "detected_at": datetime.utcnow().isoformat() + "Z"
        }
        supabase.table('detection_history').insert(data).execute()
        print(f"[Log BDD] Detection enregistrée : {det_type}",data["detected_at"])
    except Exception as e:
        print(f"[!] Erreur log detection BDD: {e}")

def est_deja_nourri(pet_id, scheduled_datetime_iso):
    try:
        response = supabase.table('feeding_history') \
            .select("id") \
            .eq("pet_id", pet_id) \
            .eq("scheduled_at", scheduled_datetime_iso) \
            .execute()
        if len(response.data) > 0:
            return True
        return False
    except Exception as e:
        print(f"[!] Erreur verification historique: {e}")
        return False

def verifier_creneaux():
    """Retourne une LISTE de schedules valides"""
    if not supabase:
        print("Pas de connexion Supabase.")
        return [] # Retourne liste vide et non None

    valid_schedules = [] # Liste pour accumuler les résultats

    try:
        response = supabase.table('feeding_schedule').select("*").eq('active', True).execute()
        schedules = response.data
        
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')

        for s in schedules:
            last_date = s.get('last_distributed_date')
            if last_date == today_str:
                continue 

            sched_time_str = s['scheduled_time'][:8] 
            try:
                sched_time = datetime.strptime(sched_time_str, "%H:%M:%S").time()
            except ValueError:
                sched_time = datetime.strptime(sched_time_str, "%H:%M").time()
            
            delay_minutes = s['detection_delay_max'] if s['detection_delay_max'] is not None else 30
            
            sched_datetime = now.replace(hour=sched_time.hour, minute=sched_time.minute, second=sched_time.second, microsecond=0)
            start_window = sched_datetime - timedelta(minutes=5)
            end_window = sched_datetime + timedelta(minutes=delay_minutes)

            if start_window <= now <= end_window:
                sched_iso = sched_datetime.isoformat()
                if est_deja_nourri(s['pet_id'], sched_iso):
                    continue 

                schedule_info = {
                    "schedule_id": s['id'],
                    "pet_id": s['pet_id'],
                    "mode": s['delivery_mode'],
                    "portion_weight": s['portion_weight'],
                    "scheduled_at": sched_iso
                }
                print(f"[+] Creneau valide trouvé pour Pet {s['pet_id']} ({s['delivery_mode']})")
                valid_schedules.append(schedule_info) # On ajoute à la liste au lieu de return
        
        return valid_schedules

    except Exception as e:
        print(f"[!] Erreur verification Supabase: {e}")
        return []

# --- FONCTIONS TRAINING (Inchangées) ---
def get_pet_in_training():
    if not supabase: return None
    try:
        response = supabase.table('pets').select("*").eq('training_mode', True).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
    except Exception as e:
        print(f"[!] Erreur check training mode: {e}")
    return None


def record_training_session(vl53, pet_info):
    """
    Enregistre le flux RTSP dans un fichier local pour traitement différé.
    """
    print(f"[Training] Démarrage enregistrement pour {pet_info['name']}...")
    
    cap = cv2.VideoCapture(RTSP_URL)
    time.sleep(1)
    
    if not cap.isOpened():
        print("[Training] Erreur : Flux RTSP HS.")
        return

    ret, first_frame = cap.read()
    if not ret:
        cap.release()
        return

    height, width = first_frame.shape[:2]
    
    # --- CHANGEMENT ICI : On écrit dans le dossier tampon ---
    timestamp = int(time.time())
    base_filename = f"train_{timestamp}"
    
    # Assurons-nous que le dossier existe
    if not os.path.exists("pending_uploads"):
        os.makedirs("pending_uploads")

    video_filename = os.path.join("pending_uploads", f"{base_filename}.avi")
    json_filename  = os.path.join("pending_uploads", f"{base_filename}.json")
    
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out = cv2.VideoWriter(video_filename, fourcc, 20.0, (width, height))
    out.write(first_frame)
    
    consecutive_absence = 0
    frames_recorded = 1
    
    print("[Training] Enregistrement... (Restez devant le capteur)")

    while True:
        try:
            distance = vl53.range
            if distance > 8000: distance = 9999
        except: distance = 9999

        ret, frame = cap.read()
        if ret:
            out.write(frame)
            frames_recorded += 1
        else:
            break
        
        if 0 < distance < SEUIL_DETECTION_MM:
            consecutive_absence = 0
        else:
            consecutive_absence += 1
        
        if consecutive_absence > NB_VALIDATIONS_STOP:
            break

    cap.release()
    out.release()
    
    print(f"[Training] Enregistrement fini ({frames_recorded} frames).")

    # --- ÉCRITURE DU FICHIER DE MÉTADONNÉES ---
    # C'est l'apparition de ce fichier JSON qui dira à l'autre script : "Go !"
    if frames_recorded > 10:
        try:
            with open(json_filename, 'w') as f:
                json.dump(pet_info, f)
            print("[Training] Fichier mis en file d'attente pour upload.")
        except Exception as e:
            print(f"[!] Erreur écriture JSON: {e}")
    else:
        print("[Training] Vidéo trop courte, supprimée.")
        if os.path.exists(video_filename):
            os.remove(video_filename)

# --- FONCTIONS D'ENVOI ---

def envoi_signal_pc_start_multi(schedules_list):
    """Envoie la LISTE des schedules au PC"""
    try:
        # On envoie directement la liste
        requests.post(URL_START_IA, json=schedules_list, timeout=2)
        print(f">>> Signal PC (IA) envoye pour {len(schedules_list)} cible(s).")
    except Exception as e:
        print(f">>> Echec envoi PC : {e}")

def envoi_signal_pc_stop():
    try:
        requests.post(URL_STOP_IA, timeout=2)
        print(f">>> Signal PC STOP envoye.")
    except Exception as e:
        print(f">>> Echec envoi STOP PC : {e}")

def envoi_signal_feeder_local(schedule_info):
    try:
        requests.post(URL_FEEDER_LOCAL, json=schedule_info, timeout=2)
        print(f">>> Signal FEEDER LOCAL envoye pour Pet {schedule_info['pet_id']}.")
    except Exception as e:
        print(f">>> Echec envoi Feeder Local : {e}")

# --- MAIN LOOP ---

def main():
    i2c = busio.I2C(board.SCL, board.SDA)
    try:
        vl53 = adafruit_vl53l0x.VL53L0X(i2c)
    except Exception as e:
        print(f"Erreur capteur: {e}")
        return

    print(f"--- Smart Surveillance Nuxi (Seuil: {SEUIL_DETECTION_MM}mm) ---")

    compteur_presence = 0
    compteur_absence = 0
    systeme_actif = False
    presence_verifiee = False
    current_mode = None 

    while True:
        try:
            try:
                distance = vl53.range
                if distance > 8000: distance = 9999
            except:
                distance = 9999

            # === CAS 1 : PRÉSENCE ===
            if 0 < distance < SEUIL_DETECTION_MM:
                compteur_presence += 1
                compteur_absence = 0 
                
                if compteur_presence >= NB_VALIDATIONS_START and not presence_verifiee:
                    
                    print(f"OK Presence physique confirmee.")
                    presence_verifiee = True 
                    
                    log_detection(det_type='tof_presence', pet_id=None, details={"distance_mm": distance})
                    
                    # Training Check
                    pet_training = get_pet_in_training()
                    if pet_training:
                        print(f"!!! MODE TRAINING DETECTE !!!")
                        record_training_session(vl53, pet_training) # Décommentez pour utiliser
                        compteur_presence = 0
                        presence_verifiee = False
                        continue 
                    
                    # Schedule Check
                    print(f"Check Schedules BDD...")
                    valid_schedules = verifier_creneaux()
                    
                    if valid_schedules:
                        # On sépare les tâches locales (Fixed Time) et distantes (IA)
                        ai_targets = []
                        
                        for s in valid_schedules:
                            if s['mode'] == 'fixed_time':
                                print(f"-> Traitement immédiat Fixed-Time (Pet {s['pet_id']})")
                                envoi_signal_feeder_local(s)
                            elif s['mode'] in ['ai', 'species_detection']:
                                ai_targets.append(s)
                        
                        # S'il reste des cibles pour l'IA, on envoie le groupe au PC
                        if ai_targets:
                            print(f"-> Envoi de {len(ai_targets)} cible(s) AI au PC.")
                            envoi_signal_pc_start_multi(ai_targets)
                            systeme_actif = True
                            current_mode = 'ai_multi'
                        else:
                            # Si tout était du fixed_time, on considère l'action faite
                            # Mais on garde systeme_actif=True pour attendre que l'animal parte
                            systeme_actif = True 
                        
                    else:
                        print("[X] Rien a faire.")

            # === CAS 2 : ABSENCE ===
            else:
                compteur_absence += 1
                compteur_presence = 0 
                
                if compteur_absence >= NB_VALIDATIONS_STOP:
                    if presence_verifiee:
                        print("X Absence confirmee.")
                        presence_verifiee = False 

                    if systeme_actif:
                        # On arrête l'IA quel que soit le mode
                        envoi_signal_pc_stop()
                        systeme_actif = False
                        current_mode = None
                    
                    if compteur_absence > 1000: compteur_absence = 100

            time.sleep(0.1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Erreur boucle: {e}")
            time.sleep(0.5)

if __name__ == "__main__":
    main()