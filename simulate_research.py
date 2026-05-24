import time
import json
import csv
import os
import base64
import random

print("🚀 Starting simulated research pipeline...")

print("⏳ Step 1: Processing data (simulating workload for 30 seconds)...")
for i in range(3):
    print(f"   ... processing batch {i+1}/3")
    time.sleep(10)

os.makedirs("artifacts", exist_ok=True)

print("📊 Step 2: Generating dataset (CSV)...")
with open("artifacts/data.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["id", "value", "category", "is_valid"])
    for i in range(1, 51):
        writer.writerow([i, round(i * 1.5, 2), "A" if i % 2 == 0 else "B", i % 3 == 0])

time.sleep(2)

print("📈 Step 3: Computing metrics (JSON)...")
metrics = {
    "accuracy": 0.95,
    "loss": 0.05,
    "training_time_seconds": 17,
    "convergence": True,
    "hyperparameters": {
        "learning_rate": 0.001,
        "batch_size": 32
    }
}
with open("artifacts/metrics.json", "w") as f:
    json.dump(metrics, f, indent=4)

time.sleep(2)

print("🖼️ Step 4: Plotting results (PNG)...")
# Minimal valid 1x1 Red PNG encoded in base64 to avoid requiring Pillow/matplotlib
png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
with open("artifacts/plot.png", "wb") as f:
    f.write(base64.b64decode(png_b64))

print("📊 Step 5: Generating random metrics table (CSV)...")
with open("artifacts/random_table.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["epoch", "loss", "accuracy"])
    for epoch in range(1, 11):
        loss = round(random.uniform(0.01, 0.5), 4)
        accuracy = round(random.uniform(0.7, 0.99), 4)
        writer.writerow([epoch, loss, accuracy])

print("🖼️ Step 6: Generating random plot (PNG with random bytes appended)...")
with open("artifacts/random_plot.png", "wb") as f:
    f.write(base64.b64decode(png_b64) + os.urandom(16))

print("✅ Research pipeline completed successfully! Artifacts written to artifacts/")
# Force DVC rerun: 2

