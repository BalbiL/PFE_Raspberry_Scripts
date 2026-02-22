import cv2
import time
import os
import threading
import sys
import argparse
import requests
import json
from datetime import datetime

# --- CONFIGURATION FIXE ---
DETECTOR_MODEL_NAME = 'yolov8n.pt' 
STREAM_URL = 'rtsp://192.168.137.54:8554/cam1'
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
IP_RASPBERRY = "192.168.137.54" 
URL_FEEDER_RASPBERRY = f"http://{IP_RASPBERRY}:5001/distribute"
TIMEOUT_SESSION = 6000 
SEUIL_CONF_ID = 0.90  
TARGET_CLASSES_FOR_ID = [15, 16, 77] 
HUMAN_CLASS = 0

# --- GESTION ARGUMENTS ---
parser = argparse.ArgumentParser()
parser.add_argument("--targets_json", required=True, help="Liste JSON des cibles")
args = parser.parse_args()

# Parsing des cibles
TARGETS = json.loads(args.targets_json)

is_distributed = False

class ThreadedCamera:
    def __init__(self, src=0):
        self.src = src
        self.capture = None
        self.status = False
        self.frame = None
        self.thread = threading.Thread(target=self.start_capture, args=())
        self.thread.daemon = True
        self.thread.start()

    def start_capture(self):
        print(f"⚡ [CAM] Tentative connexion RTSP...")
        self.capture = cv2.VideoCapture(self.src)
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if self.capture.isOpened():
            self.status = True
            _, self.frame = self.capture.read()
            while self.capture.isOpened():
                status, frame = self.capture.read()
                if status:
                    self.frame = frame
                    self.status = True
                else:
                    self.status = False
                time.sleep(0.005)
        else:
            self.status = False

    def get_frame(self):
        return self.status, self.frame

def valider_distribution(target_info, raison):
    """Envoie l'ordre pour une cible SPÉCIFIQUE"""
    global is_distributed
    if is_distributed: return
    is_distributed = True 

    print(f"\n✅ [IA] SUCCÈS ({target_info['class_name']}) : {raison}")
    print(f"📤 [IA] Ordre au Feeder pour {target_info['portion_weight']}g...")
    
    payload = {
        "schedule_id": target_info['schedule_id'],
        "pet_id": target_info['pet_id'],
        "portion_weight": float(target_info['portion_weight']),
        "scheduled_at": datetime.now().isoformat() 
    }
    
    try:
        requests.post(URL_FEEDER_RASPBERRY, json=payload, timeout=5)
        print("🚀 [IA] Ordre envoyé avec succès !")
    except Exception as e:
        print(f"❌ [IA] Erreur Feeder : {e}")
    
    print("👋 [IA] Arrêt (Un animal a été servi).")
    os._exit(0) 

def main():
    print(f"🤖 [IA] Démarrage Multi-Cibles ({len(TARGETS)} animaux)")
    
    t0 = time.time()
    cam = ThreadedCamera(STREAM_URL)
    
    # --- CHARGEMENT DES MODÈLES ---
    # On charge uniquement les modèles uniques nécessaires pour éviter les doublons en RAM
    from ultralytics import YOLO 
    detector = YOLO(DETECTOR_MODEL_NAME)
    
    classifiers = {} # Dict: "chemin/vers/model.pt" -> Objet YOLO
    
    try:
        for t in TARGETS:
            if t['mode'] == 'ai':
                path = t['model_path']
                if path not in classifiers:
                    print(f"📂 Chargement modèle : {path}")
                    classifiers[path] = YOLO(path)
    except Exception as e:
        print(f"❌ Erreur chargement modèles: {e}")
        return

    while not cam.status:
        if (time.time() - t0) > 10: 
            print("❌ [IA] Timeout Caméra")
            return
        time.sleep(0.1)

    print(f"✅ [IA] Prêt. Surveillance active...")
    
    start_session = time.time()
    last_log_time = 0
    log_interval = 2.0 

    while (time.time() - start_session) < TIMEOUT_SESSION:
        if is_distributed: break

        ret, frame = cam.get_frame()
        if not ret or frame is None:
            time.sleep(0.01)
            continue

        results = detector(frame, verbose=False, conf=0.4)[0]
        
        current_time = time.time()
        should_log = (current_time - last_log_time) > log_interval
        log_message = ""

        for box in results.boxes:
            if is_distributed: break
            cls_id = int(box.cls[0])
            
            if cls_id == HUMAN_CLASS:
                if should_log and "Humain" not in log_message: log_message += "🙋 Humain. "

            elif cls_id in TARGET_CLASSES_FOR_ID:
                # On a détecté un animal. On vérifie QUI c'est parmi nos cibles.
                
                # Extraction du crop (commun à toutes les cibles)
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                h, w = frame.shape[:2]
                crop = frame[max(0,y1-10):min(h,y2+10), max(0,x1-10):min(w,x2+10)]
                if crop.size == 0: continue

                # On teste contre CHAQUE cible active
                for target in TARGETS:
                    
                    # CAS 1 : Species Detection (N'importe quel animal suffit)
                    if target['mode'] == 'species_detection':
                        valider_distribution(target, "Espèce détectée (Mode simple)")
                        break

                    # CAS 2 : AI (Identification précise)
                    elif target['mode'] == 'ai':
                        model = classifiers.get(target['model_path'])
                        
                        if model:
                            try:
                                id_res = model(crop, verbose=False)[0]
                                top1_idx = id_res.probs.top1
                                id_name = id_res.names[top1_idx]
                                id_conf = id_res.probs.top1conf.item()

                                print(f"👀 VU: {id_name} ({id_conf:.2f}) | ATTENDU: {target['class_name']}")

                                if id_name == target['class_name'] and id_conf >= SEUIL_CONF_ID:
                                    print(f"🎯 MATCH : {id_name} ({id_conf:.1%}) pour Pet {target['pet_id']}")
                                    valider_distribution(target, f"Identifié {id_name}")
                                    break
                                elif id_name == target['class_name']:
                                    # C'est la bonne classe mais pas assez sûr
                                    if should_log: log_message += f"⚠️ Doute {id_name} ({id_conf:.0%}). "
                            except:
                                pass
        
        if log_message and should_log and not is_distributed:
            print(f"[LOG] {log_message}")
            last_log_time = current_time

        time.sleep(0.05)

    print("🛑 [IA] Fin session.")
    sys.exit(0)

if __name__ == "__main__":
    main()