#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Distributeur de croquettes – Module moteur pas à pas (NEMA17 + A4988)

Principe :
- Le réservoir est divisé en 3 compartiments
- 1 compartiment = 10 g = rotation de 120°
- Pause de 3 secondes à chaque compartiment
- Le grammage est passé en argument dans le terminal

⚠️ Sécurité :
- Seuls 20, 30 ou 40 g sont autorisés
- Sinon : erreur + driver jamais activé

Usage :
    python3 stepper_distribute.py 30
"""

import time
import math
import sys
import RPi.GPIO as GPIO

# =========================
# CONFIG GPIO (BCM)
# =========================
STEP_PIN   = 6
DIR_PIN    = 5
ENABLE_PIN = 26   # EN du A4988

# =========================
# CONFIG MECANIQUE
# =========================
GRAMS_PER_DOSE = 10
DEG_PER_DOSE = 120

# MS1 / MS2 / MS3 = GND → full step
# ⚠️ Si tu es en full-step classique NEMA17: mets 200
# Si tu as choisi 400 car 1/2 step: laisse 400
STEPS_PER_REVOLUTION = 3200
STEPS_PER_DOSE = int(STEPS_PER_REVOLUTION * DEG_PER_DOSE / 360)

# =========================
# TIMINGS
# =========================
STEP_DELAY = 0.0007        # vitesse moteur
OPEN_HOLD_SECONDS = 3.0   # temps "ouvert" par compartiment

# =========================
# GRAMMAGES AUTORISÉS
# =========================
ALLOWED_GRAMS = {10,20, 30}

# =========================
# GPIO SETUP
# =========================
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    GPIO.setup(STEP_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(DIR_PIN, GPIO.OUT, initial=GPIO.HIGH)     # sens horaire
    GPIO.setup(ENABLE_PIN, GPIO.OUT, initial=GPIO.HIGH)  # driver OFF par défaut


def enable_driver():
    GPIO.output(ENABLE_PIN, GPIO.LOW)   # A4988 : LOW = ON
    time.sleep(0.01)


def disable_driver():
    GPIO.output(ENABLE_PIN, GPIO.HIGH)  # driver OFF
    time.sleep(0.01)

# =========================
# LOW LEVEL MOTOR
# =========================
def step_pulse():
    GPIO.output(STEP_PIN, GPIO.HIGH)
    time.sleep(STEP_DELAY)
    GPIO.output(STEP_PIN, GPIO.LOW)
    time.sleep(STEP_DELAY)


def rotate_steps(steps: int):
    for _ in range(steps):
        step_pulse()

# =========================
# DISTRIBUTION LOGIC
# =========================
def dispense_one_dose():
    """
    1 dose = 120° = 10 g
    """
    rotate_steps(STEPS_PER_DOSE)
    time.sleep(OPEN_HOLD_SECONDS)


def distribute_for_grams(grams: int):
    """
    Distribue la quantité demandée (uniquement 20/30/40g).
    """
    # ✅ sécurité : valeurs autorisées uniquement
    if grams not in ALLOWED_GRAMS:
        raise ValueError(f"Grammage non autorisé: {grams} g (autorisés: 20, 30, 40)")

    doses = grams // GRAMS_PER_DOSE  # ici c'est sûr car 20/30/40
    print(f"Distribution demandée : {grams} g")
    print(f"→ {doses} compartiment(s) de 10 g")
    print(f"→ {STEPS_PER_DOSE} pas par compartiment (120°)")

    enable_driver()
    try:
        for i in range(doses):
            print(f"Dose {i+1}/{doses} : rotation 120° + pause {OPEN_HOLD_SECONDS}s")
            dispense_one_dose()

        print("Distribution terminée.")
    finally:
        disable_driver()

# =========================
# MAIN (TERMINAL)
# =========================
def main():
    setup_gpio()

    try:
        if len(sys.argv) < 2:
            print("Usage : python3 stepper_distribute.py <grams>")
            print("Valeurs autorisées : 20 | 30 | 40")
            return

        grams = int(float(sys.argv[1]))

        # ✅ validation AVANT activation driver
        if grams not in ALLOWED_GRAMS:
            print(f"[ERREUR] Grammage non autorisé : {grams} g")
            print("Valeurs autorisées : 20 | 30 | 40")
            print("→ Le moteur ne sera pas activé.")
            return

        distribute_for_grams(grams)

    except ValueError as e:
        # erreurs de parsing / validation
        print(f"[ERREUR] {e}")
        print("→ Le moteur ne sera pas activé.")

    except KeyboardInterrupt:
        print("\nArrêt demandé.")

    finally:
        GPIO.output(ENABLE_PIN, GPIO.HIGH)  # Disable driver

if __name__ == "__main__":
    main()
