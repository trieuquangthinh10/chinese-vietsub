# Chinese → Vietnamese Vietsub Web

Đây là skeleton web app cho iPhone Safari: upload video → Whisper nhận diện tiếng Trung → AI dịch theo context → FFmpeg chèn phụ đề → tải MP4.

Cần server có Python 3.10+, FFmpeg và biến môi trường OPENAI_API_KEY.

Cài:
pip install -r requirements.txt

Chạy từ thư mục backend:
python server.py

Sau đó mở http://IP-CUA-SERVER:8000 trên iPhone cùng mạng hoặc deploy lên server có HTTPS.

Không đặt API key trong frontend. Bản demo lưu job trong RAM; production nên dùng database/object storage và worker queue.
