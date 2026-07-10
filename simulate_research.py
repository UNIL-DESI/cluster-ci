import time
import json
import csv
import os
import base64
import random

# Bug corrige !
a = 1  # Correction de la division par zero


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
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    # Main plot: training curves
    epochs = list(range(1, 21))
    train_loss = [0.8 * (0.85 ** e) + random.uniform(-0.02, 0.02) for e in epochs]
    val_loss = [0.9 * (0.83 ** e) + random.uniform(-0.03, 0.03) for e in epochs]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(epochs, train_loss, 'b-o', label='Train Loss', markersize=4)
    ax.plot(epochs, val_loss, 'r-s', label='Val Loss', markersize=4)
    ax.set_xlabel('Epoch')
    ax.set_ylabel('Loss')
    ax.set_title('Training Progress')
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig("artifacts/plot.png", dpi=100)
    plt.close(fig)
    print("   ✅ plot.png generated with matplotlib")

    # Random plot: accuracy vs learning rate
    lrs = [0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1]
    accs = [round(random.uniform(0.6, 0.99), 3) for _ in lrs]
    fig2, ax2 = plt.subplots(figsize=(8, 5))
    ax2.semilogx(lrs, accs, 'g-^', markersize=8, linewidth=2)
    ax2.set_xlabel('Learning Rate')
    ax2.set_ylabel('Accuracy')
    ax2.set_title('Accuracy vs Learning Rate')
    ax2.grid(True, alpha=0.3)
    fig2.tight_layout()
    fig2.savefig("artifacts/random_plot.png", dpi=100)
    plt.close(fig2)
    print("   ✅ random_plot.png generated with matplotlib")
except ImportError:
    # Fallback: minimal valid 1x1 PNG if matplotlib is not available
    print("   ⚠️ matplotlib not available, using placeholder PNGs")
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    with open("artifacts/plot.png", "wb") as f:
        f.write(base64.b64decode(png_b64))
    with open("artifacts/random_plot.png", "wb") as f:
        f.write(base64.b64decode(png_b64) + os.urandom(16))

print("📊 Step 5: Generating random metrics table (CSV)...")
with open("artifacts/random_table.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["epoch", "loss", "accuracy"])
    for epoch in range(1, 11):
        loss = round(random.uniform(0.01, 0.5), 4)
        accuracy = round(random.uniform(0.7, 0.99), 4)
        writer.writerow([epoch, loss, accuracy])

print("✅ Research pipeline completed successfully! Artifacts written to artifacts/")
# Force DVC rerun: 4

