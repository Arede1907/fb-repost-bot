"""Video indirme testi — terminalde çalıştır: python test_download.py"""
import sys
from video_downloader import download_video, delete_video

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/shorts/LpnD_LypBaY"
video_id = url.rstrip("/").split("/")[-1]

print(f"İndiriliyor: {url}")
path = download_video(url, video_id)

if path:
    print(f"✓ Başarılı: {path}")
    delete_video(path)
    print("✓ Geçici dosya silindi")
else:
    print("✗ İndirme başarısız")
