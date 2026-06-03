#!/bin/bash
set -e

ROLE=$1

if [[ "$ROLE" == "headnode" || "$ROLE" == "worker" ]]; then
    # --- Infrastructure Deployment (Dispatcher) ---
    echo "🏗️  Cluster-CI: Infrastructure Deployment ($ROLE)"

    if ! command -v git &> /dev/null; then
        echo "❌ Error: git is not installed on this machine."
        exit 1
    fi

    INSTALL_DIR=${INSTALL_DIR:-"$HOME/cluster-ci"}
    REPO_URL="https://github.com/UNIL-DESI/cluster-ci.git"

    # Charger l'existant si disponible
    if [ -f "$INSTALL_DIR/.env" ]; then
        # On extrait proprement pour éviter de sourcer n'importe quoi
        [ -z "$GITHUB_PAT" ] && GITHUB_PAT=$(grep "^GITHUB_PAT=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
        [ -z "$HEADNODE_URL" ] && HEADNODE_URL=$(grep "^HEADNODE_URL=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
        [ -z "$CLUSTER_TOKEN" ] && CLUSTER_TOKEN=$(grep "^CLUSTER_TOKEN=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
        [ -z "$GITHUB_CLIENT_ID" ] && GITHUB_CLIENT_ID=$(grep "^GITHUB_CLIENT_ID=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
        [ -z "$GITHUB_CLIENT_SECRET" ] && GITHUB_CLIENT_SECRET=$(grep "^GITHUB_CLIENT_SECRET=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
        [ -z "$DOCKER_BASE_IMAGE" ] && DOCKER_BASE_IMAGE=$(grep "^DOCKER_BASE_IMAGE=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
    fi

    if [ "$ROLE" == "headnode" ]; then
        if [ -z "$GITHUB_PAT" ]; then
            echo "🔑 GITHUB_PAT not detected."
            read -rs -p "Please enter your GitHub PAT (with repo & workflow access): " GITHUB_PAT
            echo ""
        fi
        TARGET_REPO=$2
        if [ -z "$TARGET_REPO" ]; then
            echo "🎯 Target not detected (owner/repo or organization)."
            read -p "Please enter the GitHub target to monitor: " TARGET_REPO
        fi

        if [ -z "$GITHUB_CLIENT_ID" ]; then
            echo "🔑 GITHUB_CLIENT_ID not detected (Optional but recommended for the Dashboard)."
            read -p "Please enter the GitHub OAuth Client ID (leave empty to skip): " GITHUB_CLIENT_ID
            echo ""
        fi
        if [ -n "$GITHUB_CLIENT_ID" ] && [ -z "$GITHUB_CLIENT_SECRET" ]; then
            echo "🔑 GITHUB_CLIENT_SECRET not detected."
            read -rs -p "Please enter the GitHub OAuth Client Secret: " GITHUB_CLIENT_SECRET
            echo ""
        fi

        if [ -z "$GITHUB_PAT" ] || [ -z "$TARGET_REPO" ]; then
            echo "❌ Error: GITHUB_PAT and TARGET_REPO are required for a headnode."
            exit 1
        fi
    else
        if [ -z "$HEADNODE_URL" ]; then
            echo "🔗 HEADNODE_URL not detected."
            read -p "Please enter the Headnode URL (e.g., http://192.168.1.10:5000): " HEADNODE_URL
        fi
        if [ -z "$CLUSTER_TOKEN" ]; then
            echo "🔑 CLUSTER_TOKEN not detected (required to authenticate with the Headnode)."
            read -rs -p "Please enter the Cluster Token: " CLUSTER_TOKEN
            echo ""
        fi

        if [ -z "$HEADNODE_URL" ] || [ -z "$CLUSTER_TOKEN" ]; then
            echo "❌ Error: HEADNODE_URL and CLUSTER_TOKEN are required for a worker."
            exit 1
        fi
    fi

    # 1. Clone or update the repository
    if [ ! -d "$INSTALL_DIR" ]; then
        echo "📂 Cloning repository into $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    else
        echo "📂 Updating repository in $INSTALL_DIR..."
        cd "$INSTALL_DIR" && git pull && cd - > /dev/null
    fi

    # 2. .env configuration (selective update)
    echo "📝 Configuring environment variables..."
    mkdir -p "$INSTALL_DIR"
    TOUCH_ENV="$INSTALL_DIR/.env"
    [ ! -f "$TOUCH_ENV" ] && touch "$TOUCH_ENV"

    update_env_var() {
        local var_name=$1
        local var_value=$2
        if [ -n "$var_value" ]; then
            if grep -q "^$var_name=" "$TOUCH_ENV"; then
                # Remplacement portable de sed -i (compatible macOS/Linux)
                local tmp_env=$(mktemp)
                grep -v "^$var_name=" "$TOUCH_ENV" > "$tmp_env"
                echo "$var_name=$var_value" >> "$tmp_env"
                mv "$tmp_env" "$TOUCH_ENV"
            else
                echo "$var_name=$var_value" >> "$TOUCH_ENV"
            fi
        fi
    }

    if [ "$ROLE" == "headnode" ]; then
        if [ -z "$CLUSTER_TOKEN" ] && [ ! -f "$INSTALL_DIR/.env" ]; then
            # Génération d'un token aléatoire pour le cluster
            CLUSTER_TOKEN=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        fi
        if [ -z "$HEADNODE_URL" ]; then
            # Auto-detect the real IP address for HEADNODE_URL.
            # Docker containers in bridge mode cannot reach the host via localhost,
            # so we must use the actual network IP for dual-mode (headnode = scheduler + worker).
            DETECTED_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
            if [ -n "$DETECTED_IP" ]; then
                HEADNODE_URL="http://${DETECTED_IP}:5000"
                echo "🔗 Auto-detected headnode IP: $DETECTED_IP"
            else
                # Fallback: localhost (only works if no Docker containers need to reach the scheduler)
                HEADNODE_URL="http://localhost:5000"
                echo "⚠️  Could not detect IP, defaulting to localhost:5000"
            fi
        fi
    fi

    update_env_var "GITHUB_PAT" "$GITHUB_PAT"
    update_env_var "TARGET_REPO" "$TARGET_REPO"
    update_env_var "HEADNODE_URL" "$HEADNODE_URL"
    update_env_var "CLUSTER_TOKEN" "$CLUSTER_TOKEN"
    update_env_var "GITHUB_CLIENT_ID" "$GITHUB_CLIENT_ID"
    update_env_var "GITHUB_CLIENT_SECRET" "$GITHUB_CLIENT_SECRET"

    # Docker images: one per architecture for the heterogeneous cluster.
    # AMD64 (x86_64): Used on the headnode and any x86_64 workers.
    # ARM64 (aarch64): Used on NVIDIA ARM workers (Grace/Blackwell GB10).
    if [ -z "$DOCKER_IMAGE_AMD64" ]; then
        DOCKER_IMAGE_AMD64="nvcr.io/nvidia/pytorch:26.04-py3"
    fi
    if [ -z "$DOCKER_IMAGE_ARM64" ]; then
        DOCKER_IMAGE_ARM64="nvcr.io/nvidia/pytorch:26.04-py3"
    fi
    update_env_var "DOCKER_IMAGE_AMD64" "$DOCKER_IMAGE_AMD64"
    update_env_var "DOCKER_IMAGE_ARM64" "$DOCKER_IMAGE_ARM64"
    # Legacy fallback (kept for backward compatibility with existing workers)
    if [ -z "$DOCKER_BASE_IMAGE" ]; then
        DOCKER_BASE_IMAGE="$DOCKER_IMAGE_AMD64"
    fi
    update_env_var "DOCKER_BASE_IMAGE" "$DOCKER_BASE_IMAGE"

    # 3. Local setup execution
    echo "🚀 Starting system installation..."
    cd "$INSTALL_DIR"
    bash src/cluster/setup_runner.sh "$TARGET_REPO" "$ROLE"

    echo "✅ $ROLE deployment completed successfully in $INSTALL_DIR."

    if [ "$ROLE" == "headnode" ]; then
        IP_ADDR=$(hostname -I | awk '{print $1}')
        echo ""
        echo "🎉 Your Headnode is ready!"
        echo "👉 To add Workers, use the following command on your other machines:"
        echo "CLUSTER_TOKEN=\"$CLUSTER_TOKEN\" HEADNODE_URL=\"http://$IP_ADDR:5000\" curl -sSL $REPO_URL/raw/main/install.sh | bash -s -- worker"
        echo ""
    fi
    exit 0

else
    # --- Client-side Installation (Research Project) ---
    echo "🚀 Cluster-CI: Client Installation"

    # Detect the correct python binary to use (avoiding Windows Store stub for python3)
    LOCAL_PYTHON="python3"
    if command -v python3 &>/dev/null && python3 --version &>/dev/null; then
        LOCAL_PYTHON="python3"
    elif command -v python &>/dev/null && python --version &>/dev/null; then
        LOCAL_PYTHON="python"
    fi
    echo "🐍 Using Python binary: $LOCAL_PYTHON"

    # 0. Dependencies check (GitHub CLI)
    if ! command -v gh &> /dev/null; then
        echo "🔍 GitHub CLI (gh) not found. Attempting installation..."
        if [[ "$OSTYPE" == "linux-gnu"* ]]; then
            if command -v apt-get &> /dev/null; then
                sudo apt-get update && sudo apt-get install -y gh
            elif command -v dnf &> /dev/null; then
                sudo dnf install -y gh
            else
                echo "❌ Could not auto-install gh. Please install it manually: https://cli.github.com/"
                exit 1
            fi
        elif [[ "$OSTYPE" == "darwin"* ]]; then
            if command -v brew &> /dev/null; then
                brew install gh
            else
                echo "❌ Homebrew not found. Please install gh manually: https://cli.github.com/"
                exit 1
            fi
        fi
    fi

    # 0b. Dependencies check (uv)
    if ! command -v uv &> /dev/null; then
        echo "🔍 'uv' not found. Installing Astral uv automatically..."
        if curl -LsSf https://astral.sh/uv/install.sh | sh; then
            # Source the uv env or add to PATH for current session
            source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
            echo "✅ 'uv' installed successfully."
        else
            echo "⚠️  Failed to auto-install uv. Please install it manually: https://docs.astral.sh/uv/"
        fi
    fi

    # 1. Git environment check
    if [ ! -d ".git" ]; then
        echo "❌ Error: This script must be run at the root of a Git repository."
        exit 1
    fi

    # 2. GitHub Actions workflow injection
    echo "📦 Injecting GitHub Actions workflow..."
    mkdir -p .github/workflows

    cat <<EOF > .github/workflows/cluster-ci.yml
name: Cluster-CI Execution

# =========================================================================================
# ⚠️ AI AGENT INSTRUCTIONS (JOULES / CURSOR) ⚠️
# DO NOT MODIFY THIS FILE.
#
# This workflow is managed by the external Cluster-CI orchestrator. It executes
# the pipeline in a fully configured environment where dependencies (uv) and
# SECRETS are automatically injected by the runner.
#
# If you need to run tests or research scripts, DO NOT create a separate workflow
# file running native shell commands (like 'pytest' or 'uv run ...'). Native steps
# won't have access to the cluster's secure environment variables.
#
# INSTEAD:
# 1. Create a DVC stage in 'dvc.yaml' (e.g., 'dvc stage add -n test pytest ...').
# 2. Put the stage name(s) in the '.cluster-ci' file at the root of the repo.
# 3. The orchestrator will automatically run 'dvc repro <stage>' with all secrets loaded.
# =========================================================================================

on:
  push:
    branches: [ main, master, cluster-draft/* ]
  pull_request:
    branches: [ main, master ]
  workflow_dispatch:

concurrency:
  group: \${{ github.workflow }}-\${{ github.ref }}
  cancel-in-progress: true

jobs:
  execute-on-cluster:
    name: Execute Research Pipeline
    runs-on: [self-hosted, cluster-worker]
    steps:
      - name: Checkout Code
        uses: actions/checkout@v5
        with:
          fetch-depth: 0

      - name: Run Orchestrator
        env:
          ALL_GITHUB_SECRETS: \${{ toJSON(secrets) }}
        run: /usr/local/bin/cluster-ci-run "\${{ github.repository }}" "\${{ github.head_ref || github.ref_name }}" "\${{ secrets.GITHUB_TOKEN }}"
EOF

    # 3. .cluster-ci configuration file injection
    if [ ! -f ".cluster-ci" ]; then
        echo "📄 Creating .cluster-ci file..."
        cat <<EOF > .cluster-ci
# =========================================================================================
# ⚠️ AI AGENT INSTRUCTIONS (JOULES / CURSOR) ⚠️
# Configuration pour Cluster-CI.
#
# REQUIRED_RAM: Contrainte de placement RAM (ex: 16GB). Défaut: 2GB.
# REQUIRED_VRAM: Contrainte de placement VRAM GPU (ex: 24GB). Défaut: 0.
# MAX_RUNTIME_HOURS: Durée maximale du job (max 24h). OBLIGATOIRE.
# EXPOSED_PORT: Port à exposer (ex: 8501). Active le routage pour une interface web.
#
# Liste ensuite les stages DVC à exécuter (un par ligne ou séparés par des espaces).
# Laisse vide après les variables pour tout exécuter (dvc repro).
# =========================================================================================
REQUIRED_RAM=2GB
REQUIRED_VRAM=0GB
MAX_RUNTIME_HOURS=1

EOF
        echo "✅ .cluster-ci file created."
    else
        echo "⚠️ .cluster-ci file already present, not overwritten."
    fi
    # 4. Pre-flight Scanner & Pre-commit Hook
    echo "🔍 Setting up Pre-flight Scanner & Pre-commit Hook..."
    mkdir -p .cluster-ci-tools
    
    # Download tools from the orchestrator repo (using raw content from GitHub)
    # Note: In a real scenario, REPO_URL would be used.
    # For this implementation, we copy them from the current project structure if they exist locally,
    # or we simulate the download.
    ORCHESTRATOR_REPO="UNIL-DESI/cluster-ci"
    RAW_URL="https://raw.githubusercontent.com/$ORCHESTRATOR_REPO/main"
    
    # Simulate download or copy if local (for development)
    if [ -f "$(dirname "$0")/src/runner/validate_pyproject.py" ]; then
        cp "$(dirname "$0")/src/runner/validate_pyproject.py" .cluster-ci-tools/
        cp "$(dirname "$0")/cluster_constraints.txt" .cluster-ci-tools/
    else
        curl -sSL "$RAW_URL/src/runner/validate_pyproject.py" -o .cluster-ci-tools/validate_pyproject.py
        curl -sSL "$RAW_URL/cluster_constraints.txt" -o .cluster-ci-tools/cluster_constraints.txt
    fi

    # Install dependencies for the validator
    echo "📦 Installing validator dependencies (tomlkit)..."
    $LOCAL_PYTHON -c "import tomlkit" 2>/dev/null || $LOCAL_PYTHON -m pip install tomlkit --user || true

    # Inject Hook
    HOOK_FILE=".git/hooks/pre-commit"
    echo "🪝 Injecting Git pre-commit hook..."
    
    cat <<EOF > "$HOOK_FILE"
#!/bin/bash
# Cluster-CI Pre-commit Validator

INTERACTIVE=""
if [ -t 1 ] && [ -c /dev/tty ] 2>/dev/null; then
  exec < /dev/tty
  INTERACTIVE="--interactive"
fi

if command -v python3 >/dev/null 2>&1 && python3 --version >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1 && python --version >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "❌ Error: Python is not installed or not in PATH."
  exit 1
fi

\$PYTHON_CMD .cluster-ci-tools/validate_pyproject.py \$INTERACTIVE --pyproject pyproject.toml --constraints .cluster-ci-tools/cluster_constraints.txt
EOF
    chmod +x "$HOOK_FILE"
    echo "✅ Pre-commit hook installed."

    # 5. Instructions in AGENTS.md
    echo "📝 Updating AGENTS.md..."
    if [ ! -f "AGENTS.md" ]; then
        touch "AGENTS.md"
        echo "# Règles de l'Agent pour ce Répertoire" > "AGENTS.md"
        echo "" >> "AGENTS.md"
    fi

    # Portable in-place sed removal of old section
    sed -i.bak '/<!-- CLUSTER-CI:START -->/,/<!-- CLUSTER-CI:END -->/d' "AGENTS.md" && rm -f "AGENTS.md.bak"

    cat << 'EOF' >> "AGENTS.md"
<!-- CLUSTER-CI:START -->
## ⚠️ Cluster-CI — Instructions Agent

Ce dépôt utilise **Cluster-CI** : un cluster ARM64 NVIDIA (GPU Blackwell GB10, 128GB RAM, Ubuntu 24.04).

### 🚫 Règle n°1 — NE TOUCHE PAS à l'infrastructure cluster

Les dossiers et fichiers suivants sont gérés automatiquement par le cluster. **Ne les modifie pas, ne les débogue pas, n'essaie pas de les "réparer"** :
- `.cluster-ci-tools/`, `.cluster-ci-logs/`, `.dvc/`, `.dvc-viewer/`
- `.github/workflows/cluster-ci.yml`
- Tout script temporaire installé par le système (hooks, bootstrap, viewers, hashes)

Si tu vois des messages inhabituels dans les logs de `cluster-run` concernant ces composants (DVC viewer, hashes, mémoire, queues, allocation), **c'est normal** — ignore-les.

### 🐍 Environnement d'exécution

- **Python 3.12** — `requires-python` doit inclure `3.12` (ex: `>=3.10`).
- **PyTorch 2.12 + CUDA 13.2** pré-installés dans le conteneur NGC. Ne mets **aucune version fixe** (`==`) pour `torch`, `torchvision`, etc. Utilise `torch` sans version ou `>=2.0`.
- Tu peux utiliser les dernières versions de toutes les autres bibliothèques.

### 🔬 Pipeline DVC + `.cluster-ci`

Toute exécution (tests, entraînement, expériences) **DOIT passer par un stage DVC** dans `dvc.yaml`.

**Déclarer un stage** :
```yaml
stages:
  my_experiment:
    cmd: python3 src/run.py --epochs 10
    deps: [src/run.py]
    outs: [results/model.pt]
    metrics: [results/metrics.json: {cache: false}]
    plots: [results/plot.png: {cache: false}]
```

**Fichier `.cluster-ci`** (paramètres d'exécution) :
```env
REQUIRED_RAM=2GB
REQUIRED_VRAM=24GB
MAX_RUNTIME_HOURS=1
```
- `MAX_RUNTIME_HOURS` (max 24) : **obligatoire**.
- `REQUIRED_RAM` : contrainte de placement RAM (défaut: 2GB).
- `REQUIRED_VRAM` : contrainte de placement VRAM GPU (défaut: 0, pas de contrainte). Le scheduler n'assignera le job qu'à des workers disposant d'au moins cette quantité de VRAM.
- `EXPOSED_PORT=<port>` : pour interfaces web (Gradio, Streamlit, TensorBoard).
- `STAGES` : **laisser vide par défaut** → exécute toute la pipeline (`dvc repro`), optimal pour le debug progressif. Si besoin de cibler un sous-ensemble, indique uniquement le **dernier stage voulu** — DVC réexécutera automatiquement les dépendances nécessaires.
- Les secrets GitHub Repository sont automatiquement transmis au cluster.

### 📊 Outs/Deps vs Métriques/Plots — Deux circuits distincts

| | `deps` / `outs` | `metrics` / `plots` |
|---|---|---|
| **Stockage** | Peer-to-peer géré par le cluster | Synchronisés via Git |
| **À gérer ?** | Non — le cluster s'en charge | Oui — les déclarer avec `cache: false` |
| **Recommandation** | Déclare-les dans `dvc.yaml`, ne t'occupe pas de leur transfert | **Au moins 1 métrique + 1 plot par stage** pour le suivi |

**Conflits Git attendus** : Les métriques et plots sont automatiquement committés par le cluster à chaque nouveau résultat (commits sans déclenchement CI). Lors d'un `git pull`, des conflits sur ces fichiers sont **normaux et voulus**. Choisis simplement quelle version garder (résultats locaux via `cluster-run` ou résultats du dernier commit automatique).

### 🚀 CLI `cluster-run`

Pour tester/itérer sans attendre GitHub, utilise `cluster-run` dans ton terminal :
- `cluster-run` : pousse un shadow commit, déclenche l'exécution et **stream les logs en temps réel**.
- `cluster-run list` : statut des runs récents.
- `cluster-run view [id]` : reprend le streaming d'un run.
- `cluster-run cancel [id]` : annule un run en cours.

**Jamais de SSH direct** sur le cluster. Toujours passer par `cluster-run`.
<!-- CLUSTER-CI:END -->
EOF
    echo "✅ AGENTS.md updated."

    # 6. Install cluster-run CLI (Python unified version)
    echo "🛠️  Installing cluster-run CLI..."
    mkdir -p "$HOME/.local/bin"

    # Download the script from the orchestrator repo
    if [ -f "$(dirname "$0")/src/cluster/cluster_run.py" ]; then
        cp "$(dirname "$0")/src/cluster/cluster_run.py" "$HOME/.local/bin/cluster-run"
    else
        curl -sSL "$RAW_URL/src/cluster/cluster_run.py" -o "$HOME/.local/bin/cluster-run"
    fi

    # Fix line endings to prevent shebang issues in Git Bash on Windows
    sed -i.bak 's/\r$//' "$HOME/.local/bin/cluster-run" 2>/dev/null || true
    rm -f "$HOME/.local/bin/cluster-run.bak"

    # If LOCAL_PYTHON is python (on Windows), replace the python3 shebang with python
    if [ "$LOCAL_PYTHON" = "python" ]; then
        sed -i.bak '1s/python3/python/' "$HOME/.local/bin/cluster-run" 2>/dev/null || true
        rm -f "$HOME/.local/bin/cluster-run.bak"
    fi

    chmod +x "$HOME/.local/bin/cluster-run"

    # Create Windows wrapper for PowerShell/CMD
    cat << 'EOF' > "$HOME/.local/bin/cluster-run.cmd"
@echo off
python "%~dp0cluster-run" %*
EOF


    # Add ~/.local/bin to PATH if not already there
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        SHELL_CONFIG=""
        if [[ "$SHELL" == */zsh ]]; then SHELL_CONFIG="$HOME/.zshrc"; else SHELL_CONFIG="$HOME/.bashrc"; fi
        if [ -f "$SHELL_CONFIG" ]; then
            if ! grep -q ".local/bin" "$SHELL_CONFIG"; then
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
                echo "💡 Added ~/.local/bin to $SHELL_CONFIG. Please restart your shell or run: source $SHELL_CONFIG"
            fi
        fi
    fi

    # Add ~/.local/bin to Windows User PATH if running under MSYS/Cygwin (Windows Git Bash)
    if [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
        WINDOWS_BIN_DIR=$(cygpath -w "$HOME/.local/bin" 2>/dev/null || echo "$USERPROFILE\\.local\\bin")
        echo "🪟 Windows system detected. Registering $WINDOWS_BIN_DIR in Windows User PATH..."
        powershell.exe -NoProfile -Command "
            \$binDir = '$WINDOWS_BIN_DIR';
            \$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User');
            \$paths = \$currentPath -split ';';
            if (\$paths -notcontains \$binDir) {
                [Environment]::SetEnvironmentVariable('Path', \$currentPath + ';' + \$binDir, 'User');
                write-output '💡 Added ~/.local/bin to Windows User PATH. You may need to restart your terminal or editor (VS Code, etc.) for changes to take effect.';
            } else {
                write-output '✅ ~/.local/bin is already in Windows User PATH.';
            }
        " 2>/dev/null || echo "⚠️ Failed to automatically update Windows User PATH. Please add $WINDOWS_BIN_DIR to your Windows environment PATH variables manually."
    fi

    echo ""
    echo "🎉 Installation complete!"
    echo "👉 Remember to commit and push the generated files:"
    echo "   git add .github/workflows/cluster-ci.yml .cluster-ci AGENTS.md"
    echo "   git commit -m \"docs: cluster-ci integration\""
    echo "   git push"
    echo ""
    echo "💡 Note for Joules AI: Any modification to the research pipeline must be reflected in .cluster-ci if you want to limit execution to specific stages."
fi
