import time
import os
import json
import cv2
import boto3
from datetime import datetime

# --- CONFIGURATION ---
UPLOAD_DIR = "pending_uploads"
AWS_ACCESS_KEY = "XXXX"
AWS_SECRET_KEY = "XXXX"
BUCKET_NAME = "pfe-pets-dataset-storage"

# Init S3
try:
    s3 = boto3.client('s3', aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)
except Exception as e:
    print(f"❌ Erreur init S3: {e}")
    s3 = None

def process_video_file(video_path, metadata_path):
    """Traite une vidéo et l'upload"""
    try:
        # 1. Lire les infos du pet (stockées dans le JSON associé)
        with open(metadata_path, 'r') as f:
            pet_info = json.load(f)
        
        print(f"🔄 Traitement en cours pour {pet_info['name']}...")

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"⚠️ Vidéo illisible : {video_path}")
            return

        frame_count = 0
        upload_count = 0
        
        # Structure S3 : dataset/{user_id}/{Name}_{pet_id}/
        folder_name = f"{pet_info['name']}_{pet_info['id']}"
        base_s3_path = f"dataset/{pet_info['user_id']}/{folder_name}"

        while True:
            ret, frame = cap.read()
            if not ret: break
            
            # 1 image toutes les 10 frames
            if frame_count % 10 == 0:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{timestamp}_{frame_count}.jpg"
                s3_key = f"{base_s3_path}/{filename}"
                
                try:
                    _, buffer = cv2.imencode('.jpg', frame)
                    s3.put_object(
                        Bucket=BUCKET_NAME, 
                        Key=s3_key, 
                        Body=buffer.tobytes(),
                        ContentType='image/jpeg'
                    )
                    upload_count += 1
                except Exception as e:
                    print(f"⚠️ Erreur S3 frame {frame_count}: {e}")
            
            frame_count += 1
        
        cap.release()
        print(f"✅ Terminé : {upload_count} images envoyées.")

        # 2. Nettoyage : On supprime vidéo ET json
        os.remove(video_path)
        os.remove(metadata_path)
        
    except Exception as e:
        print(f"❌ Erreur générale processing : {e}")
        # En cas d'erreur, on renomme pour ne pas boucler à l'infini
        os.rename(video_path, video_path + ".error")
        os.rename(metadata_path, metadata_path + ".error")

def main():
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR)
    
    print(f"👀 Upload Manager surveille le dossier '{UPLOAD_DIR}'...")

    while True:
        # On cherche les fichiers .json (qui signalent qu'une vidéo est prête)
        files = os.listdir(UPLOAD_DIR)
        json_files = [f for f in files if f.endswith('.json')]

        if json_files:
            for json_file in json_files:
                # On déduit le nom de la vidéo : "video_123.avi" pour "video_123.json"
                base_name = os.path.splitext(json_file)[0]
                video_file = base_name + ".avi"
                
                full_json_path = os.path.join(UPLOAD_DIR, json_file)
                full_video_path = os.path.join(UPLOAD_DIR, video_file)

                if os.path.exists(full_video_path):
                    # C'est parti !
                    process_video_file(full_video_path, full_json_path)
                else:
                    # JSON orphelin (vidéo pas encore finie d'écrire ou manquante)
                    pass
        
        # Petite pause pour ne pas saturer le CPU
        time.sleep(2)

if __name__ == "__main__":
    main()