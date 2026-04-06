from flask import Flask, jsonify


app = Flask(__name__)


@app.route("/system/services/airflow-postgres66/exposed/")
def live_probe():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
