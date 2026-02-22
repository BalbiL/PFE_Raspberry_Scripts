from flask import Flask, request
import subprocess
import sys
import os
import json 
from flask_cors import CORS


app = Flask(__name__)
CORS(app)

# --- GESTION DES PROCESSUS ---
process_ia = None     # Pour l'inférence (Reconnaissance temps réel)
process_train = None  # Pour l'entraînement (Apprentissage)

# --- CONFIGURATION CHEMINS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOSSIER_IA_NAME = "IA-chat"
CWD_PATH = os.path.join(BASE_DIR, DOSSIER_IA_NAME)

# Noms des scripts
NOM_SCRIPT_IA = "dri_headless.py" 
NOM_SCRIPT_TRAIN = "train_yolo_classifier.py" # <--- NOUVEAU

VENV_PYTHON = os.path.join(CWD_PATH, "venv", "Scripts", "python.exe")
ENV_TRAINING_SCRIPT=sys.executable

if not os.path.exists(VENV_PYTHON):
    print("❌ ERREUR CRITIQUE : Python introuvable !")
    sys.exit(1)

# --- MAPPING : UUID -> MODÈLE ---
# (Ce mapping devra être mis à jour dynamiquement ou via BDD idéalement après un entraînement)
PET_CONFIG = {
    "5050c3f6-ef57-4ef1-8156-6342cac6f57e": {
        "model_file": "runs/classify/yolo_cat_classifier/weights/best.pt",
        "class_name": "target_cat" 
    },
    "0c61caae-8d8d-4b4b-bf99-5ef5803a9e5a": {
        "model_file": "runs/classify/yolo_cat_classifier/weights/chat2.pt",
        "class_name": "chat2" 
    }
}

# --- ROUTES EXISTANTES (INFERENCE) ---

@app.route('/trigger-ai', methods=['POST'])
def trigger_ai():
    global process_ia
    
    data_list = request.get_json(silent=True)
    if not data_list:
        return "Donnees manquantes", 400
    
    if not isinstance(data_list, list):
        data_list = [data_list]

    print(f"\n📨 [SERVEUR] Signal START reçu pour {len(data_list)} cible(s).")
    
    targets_args = []
    
    for item in data_list:
        pet_id = item.get('pet_id')
        config = PET_CONFIG.get(pet_id)
        if not config:
            print(f"⚠️ Pas de config modèle pour {pet_id}, ignoré.")
            continue
            
        target_obj = {
            "pet_id": pet_id,
            "model_path": config['model_file'],
            "class_name": config['class_name'],
            "mode": item.get('mode'),
            "portion_weight": item.get('portion_weight'),
            "schedule_id": item.get('schedule_id')
        }
        targets_args.append(target_obj)

    if not targets_args:
        print("❌ Aucune cible valide configurée.")
        return "Erreur config", 400

    if process_ia is not None:
        if process_ia.poll() is None:
            print("⚠️ [SERVEUR] L'IA tourne déjà.")
            return "IA deja en cours", 200

    print(f"🚀 [SERVEUR] Lancement IA Multi-Cibles...")
    
    json_targets = json.dumps(targets_args)
    
    cmd = [
        VENV_PYTHON, "-u", NOM_SCRIPT_IA,
        "--targets_json", json_targets
    ]

    try:
        process_ia = subprocess.Popen(
            cmd,
            cwd=CWD_PATH, 
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        return "IA Lancee", 200
    except Exception as e:
        print(f"❌ Erreur lancement: {e}")
        return str(e), 500

@app.route('/stop-ai', methods=['POST'])
def stop_ai():
    global process_ia
    print("\n🛑 [SERVEUR] Signal STOP reçu.")
    if process_ia is not None and process_ia.poll() is None:
        process_ia.terminate() 
        process_ia = None 
        return "IA Arretée", 200
    else:
        return "Rien a arreter", 200


# --- NOUVELLE ROUTE (ENTRAINEMENT) ---

@app.route('/train-model', methods=['POST'])
def train_model():
    global process_train
    print("test")
    
    # 1. Récupération des données
    data = request.get_json(silent=True)
    if not data:
        return "Donnees manquantes", 400
        
    pet_uuid = data.get('pet_uuid')
    if not pet_uuid:
        print("❌ [SERVEUR] Erreur: UUID manquant pour l'entraînement.")
        return "UUID manquant", 400

    # 2. Vérification : un entraînement est-il déjà en cours ?
    if process_train is not None:
        if process_train.poll() is None: # None = le processus tourne encore
            print("⚠️ [SERVEUR] Un entraînement est déjà en cours.")
            return "Entrainement deja en cours", 409 # 409 Conflict

    print(f"\n🏋️ [SERVEUR] Demande d'entraînement reçue pour : {pet_uuid}")
    print(f"🚀 [SERVEUR] Lancement du script d'entraînement...")

    # 3. Construction de la commande
    # python train_yolo_classifier.py --pet_uuid <UUID>
    cmd = [
        ENV_TRAINING_SCRIPT, "-u", NOM_SCRIPT_TRAIN,
        "--pet_uuid", pet_uuid
    ]

    try:
        # 4. Lancement asynchrone (Popen ne bloque pas le serveur)
        process_train = subprocess.Popen(
            cmd,
            cwd=CWD_PATH, # Important : on s'exécute dans le dossier IA-chat
            stdout=sys.stdout, # On redirige les logs vers la console du serveur
            stderr=sys.stderr
        )
        return f"Entrainement lance pour {pet_uuid}", 200

    except Exception as e:
        print(f"❌ Erreur lancement entraînement: {e}")
        return str(e), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)