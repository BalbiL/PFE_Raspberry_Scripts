"""
Script d'entraînement YOLOv8 Classification (S3 Auto-Download + Supabase Update)
================================================================================
Usage: python train_yolo_classifier.py --pet_uuid <UUID>
"""

import os
import shutil
import random
import sys
import argparse
import boto3
from ultralytics import YOLO
from supabase import create_client, Client, ClientOptions

# --- CONFIGURATION AWS ---
AWS_ACCESS_KEY = "XXXX"
AWS_SECRET_KEY = "XXXXX"
BUCKET_NAME = "XXXXX"

# --- CONFIGURATION SUPABASE ---
SUPABASE_URL = "https://ulznwgmstojtkroyggmb.supabase.co"
SUPABASE_KEY = "XXXX"

# --- CONFIGURATION LOCALE ---
RAW_DATA_DIR = os.path.join('data', 'raw')
PROCESSED_DATA_DIR = os.path.join('data', 'processed')

# Nom du dossier "négatif" sur S3
S3_OTHER_PATH = "dataset/other" 

def get_s3_client():
    return boto3.client(
        's3',
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY
    )

def get_supabase_client():
    try:
        options = ClientOptions(postgrest_client_timeout=10)
        return create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
    except Exception as e:
        print(f"❌ Erreur init Supabase: {e}")
        return None

def mark_pet_as_trained(pet_uuid):
    """Met à jour la colonne is_model_trained à True dans la BDD"""
    supabase = get_supabase_client()
    if not supabase: return

    try:
        data = supabase.table('pets') \
            .update({'is_model_trained': True}) \
            .eq('id', pet_uuid) \
            .execute()
        print(f"✅ BDD mise à jour : is_model_trained = True pour {pet_uuid}")
    except Exception as e:
        print(f"❌ Erreur mise à jour BDD : {e}")

def find_pet_s3_path(s3, pet_uuid):
    print(f"🔍 Recherche du dossier pour l'UUID : {pet_uuid}...")
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix='dataset/')
    
    target_prefix = None
    for page in pages:
        if 'Contents' not in page: continue
        for obj in page['Contents']:
            key = obj['Key']
            if f"_{pet_uuid}/" in key:
                folder_part = key.split(f"_{pet_uuid}/")[0] + f"_{pet_uuid}/"
                target_prefix = folder_part
                break
        if target_prefix: break
            
    if target_prefix:
        print(f"✅ Dossier trouvé sur S3 : {target_prefix}")
        return target_prefix
    else:
        print(f"❌ Impossible de trouver un dossier contenant {pet_uuid}")
        return None

def download_folder_from_s3(s3, s3_prefix, local_dir):
    print(f"📥 Téléchargement de s3://{BUCKET_NAME}/{s3_prefix} vers {local_dir}...")
    if os.path.exists(local_dir):
        shutil.rmtree(local_dir)
    os.makedirs(local_dir, exist_ok=True)
    
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix=s3_prefix)
    
    count = 0
    for page in pages:
        if 'Contents' not in page: continue
        for obj in page['Contents']:
            key = obj['Key']
            if key.endswith('/'): continue 
            filename = os.path.basename(key)
            local_file_path = os.path.join(local_dir, filename)
            s3.download_file(BUCKET_NAME, key, local_file_path)
            count += 1
    print(f"✅ {count} images téléchargées.")

def prepare_data(target_class, other_class='other', split_ratio=(0.7, 0.2, 0.1)):
    print(f"🔄 Préparation des datasets (Classe cible: {target_class})...")
    
    if os.path.exists(PROCESSED_DATA_DIR):
        shutil.rmtree(PROCESSED_DATA_DIR)
    
    classes_to_use = [target_class, other_class]

    for cls in classes_to_use:
        src_dir = os.path.join(RAW_DATA_DIR, cls)
        if not os.path.exists(src_dir) or not os.listdir(src_dir):
            print(f"⚠️ ERREUR: Pas d'images pour la classe '{cls}' dans {src_dir}")
            return False

        images = [f for f in os.listdir(src_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        random.shuffle(images)
        
        n = len(images)
        idx_train = int(n * split_ratio[0])
        idx_val = int(n * (split_ratio[0] + split_ratio[1]))
        
        splits = {
            'train': images[:idx_train],
            'val': images[idx_train:idx_val],
            'test': images[idx_val:]
        }
        
        for split, split_images in splits.items():
            dest_dir = os.path.join(PROCESSED_DATA_DIR, split, cls)
            os.makedirs(dest_dir, exist_ok=True)
            for img in split_images:
                shutil.copy(os.path.join(src_dir, img), os.path.join(dest_dir, img))
    
    print("✅ Données splittées et prêtes.")
    return True

def train_model(project_name):
    model = YOLO('yolov8n-cls.pt') 
    print(f"🚀 Lancement de l'entraînement : {project_name}")
    
    # Correction chemin absolu pour éviter les problèmes de dossier runs/classify/models
    project_dir = os.path.join(os.getcwd(), 'models')
    
    model.train(
        data=PROCESSED_DATA_DIR,
        epochs=15,
        imgsz=224,
        project=project_dir,
        name=project_name,
        exist_ok=True
    )
    print(f"✅ Modèle sauvegardé : {os.path.join(project_dir, project_name, 'weights', 'best.pt')}")

def main():
    parser = argparse.ArgumentParser(description="Train YOLOv8 from S3 Data")
    parser.add_argument("--pet_uuid", required=True, help="UUID du pet cible")
    args = parser.parse_args()
    
    target_uuid = args.pet_uuid
    TARGET_CLASS_NAME = f"target_{target_uuid}"
    
    print(f"🏷️  Nom de la classe cible : {TARGET_CLASS_NAME}")
    
    # 1. Connexion S3
    s3 = get_s3_client()
    
    # 2. Trouver le chemin S3
    target_s3_path = find_pet_s3_path(s3, target_uuid)
    if not target_s3_path:
        sys.exit(1)
        
    # 3. Télécharger les images
    download_folder_from_s3(s3, target_s3_path, os.path.join(RAW_DATA_DIR, TARGET_CLASS_NAME))
    download_folder_from_s3(s3, S3_OTHER_PATH, os.path.join(RAW_DATA_DIR, 'other'))
    
    # 4. Préparer les données
    if prepare_data(TARGET_CLASS_NAME, 'other'):
        # 5. Entraîner
        train_model(f"yolo_{target_uuid}")
        
        # 6. Mettre à jour la BDD (NOUVEAU)
        mark_pet_as_trained(target_uuid)
        
    else:
        print("❌ Échec de la préparation des données.")

if __name__ == '__main__':
    main()