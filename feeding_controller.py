from flask import Flask, request
from datetime import datetime, timedelta # Import optimisé
import subprocess
import os
import sys
from flask_cors import CORS
from supabase import create_client, Client, ClientOptions

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION CHEMINS (RASPBERRY PI) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_MOTEUR = os.path.join(BASE_DIR, "test_nema.py")
VENV_PYTHON = os.path.join(BASE_DIR, ".venv", "bin", "python")

# --- CONFIGURATION SUPABASE ---
SUPABASE_URL = "https://ulznwgmstojtkroyggmb.supabase.co"
SUPABASE_KEY = "XXXXX"

try:
    options = ClientOptions(postgrest_client_timeout=5)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
except Exception as e:
    print(f"Warning Erreur init Supabase: {e}")
    supabase = None

@app.route('/distribute', methods=['POST'])
def distribute():
    data = request.get_json(silent=True)
    if not data:
        return "Donnees manquantes", 400
    
    # --- DEBUG ---
    print(f"🔍 [DEBUG] Payload reçu : {data}") 
    # -------------

    schedule_id = data.get('schedule_id')
    pet_id = data.get('pet_id')
    weight = data.get('portion_weight') 
    scheduled_at_str = data.get('scheduled_at') # On récupère la chaine

    # Heure locale de la Pi
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    print("---------------------------------------------------------------")
    print(f"🍽️  [FEEDER] ACTION : {now.strftime('%H:%M:%S')}")
    print(f"    Animal ID : {pet_id}")
    print(f"    Poids     : {weight}g")
    print("---------------------------------------------------------------")
    
    # --- 1. ACTIVATION DU MOTEUR PHYSIQUE ---
    if weight is not None:
        try:
            if not os.path.exists(VENV_PYTHON):
                print(f"❌ ERREUR : Python venv introuvable ici : {VENV_PYTHON}")
            elif not os.path.exists(SCRIPT_MOTEUR):
                print(f"❌ ERREUR : Script moteur introuvable ici : {SCRIPT_MOTEUR}")
            else:
                print(f"⚙️  Lancement du moteur pour {weight}g...")
                cmd = [VENV_PYTHON, SCRIPT_MOTEUR, str(weight)]
                subprocess.Popen(cmd)
                print("✅ Commande moteur envoyée.")

        except Exception as e:
            print(f"❌ Erreur critique lancement moteur : {e}")
    else:
        print("⚠️ Pas de poids spécifié, le moteur ne tournera pas.")

    # --- 2. GESTION BDD (Verrouillage & Historique) ---
    if supabase:
        try:
            # A. Verrouillage du Schedule
            if schedule_id:
                supabase.table('feeding_schedule') \
                    .update({'last_distributed_date': today_str}) \
                    .eq('id', schedule_id) \
                    .execute()
                print("[V] Schedule mis a jour (Verrouille pour aujourd'hui).")
            else:
                print("[!] Pas de schedule_id, pas de verrouillage.")

            # B. Ajout Historique (Avec correction temporelle)
            if scheduled_at_str and weight is not None:
                
                final_delivered_at = now.isoformat()

                # --- CORRECTION SYNC HORLOGE ---
                try:
                    # On convertit la string reçue en objet datetime pour comparer
                    # On remplace le 'Z' par +00:00 pour la compatibilité ISO
                    clean_sched_str = scheduled_at_str.replace('Z', '+00:00')
                    scheduled_dt = datetime.fromisoformat(clean_sched_str)
                    
                    # Si 'now' (Pi) est plus vieux que 'scheduled' (PC) -> BUG BDD
                    # On compare en ignorant le timezone (naive) ou en le gérant si présent
                    # Pour faire simple : on convertit scheduled_dt en naive pour comparer avec now
                    scheduled_dt_naive = scheduled_dt.replace(tzinfo=None)

                    if now <= scheduled_dt_naive:
                        print(f"⚠️ [TIME SYNC] Retard détecté ({now} <= {scheduled_dt_naive})")
                        print("   -> Correction forcée de delivered_at pour satisfaire la BDD.")
                        # On force delivered_at à être scheduled_at + 100ms
                        corrected_dt = scheduled_dt + timedelta(milliseconds=100)
                        final_delivered_at = corrected_dt.isoformat()
                
                except Exception as e:
                    print(f"⚠️ Erreur parsing date ({e}), utilisation heure locale brute.")

                data_history = {
                    "pet_id": pet_id,
                    "scheduled_at": scheduled_at_str,
                    "delivered_at": final_delivered_at, # <--- On utilise la date corrigée
                    "served_weight": weight
                }
                supabase.table('feeding_history').insert(data_history).execute()
                print("[V] Historique ajoute.")

        except Exception as e:
            print(f"[!] Erreur BDD: {e}")
    
    return "Distribution OK", 200

if __name__ == '__main__':
    print("Feeder Controller pret sur le port 5001...")
    if not os.path.exists(VENV_PYTHON):
        print(f"⚠️ ATTENTION : Le venv semble introuvable à : {VENV_PYTHON}")
    else:
        print(f"✅ Venv détecté : {VENV_PYTHON}")
        
    app.run(host='0.0.0.0', port=5001)