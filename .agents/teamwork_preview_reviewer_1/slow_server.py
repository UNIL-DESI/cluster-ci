import time
from flask import Flask

app = Flask(__name__)

@app.route('/job_status/<job_id>')
def job_status(job_id):
    time.sleep(30)
    return {"status": "pending", "worker_service_url": "http://localhost:8080"}

if __name__ == '__main__':
    app.run(port=5000)
