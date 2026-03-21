# Valentine AI Agent - Comprehensive Setup Guide (Oracle ARM64 + Qwen3 8B)

This document is the **definitive, step-by-step master guide** for setting up your personal AI agent, **Valentine**, on an Oracle Cloud "Always Free" VM. 

Given your target server configuration -- **2 OCPUs (Ampere A1 aarch64), 12GB RAM, and no dedicated GPU** -- this guide is specifically optimized for **ARM CPU-only inference** utilizing **Ollama** and the highly efficient **Qwen3 8B** model.

---

## 🏗️ Phase 1: Oracle Cloud Infrastructure (OCI) Provisioning

*(If you have already created the instance and can SSH into it, skip to Phase 2).*

### 1.1 Creating the Instance (Ampere A1 Flex)
1. **Login** to your Oracle Cloud Console.
2. Navigate to **Compute > Instances** and click **Create Instance**.
3. **Name:** `valentine-server`.
4. **Image and Shape:**
   * **Image:** Click Edit -> Change Image -> Ubuntu -> **Ubuntu 22.04 Minimal aarch64** (ARM).
   * **Shape:** Click Edit -> Change Shape -> Ampere -> **VM.Standard.A1.Flex**.
   * **OCPUs:** Set the slider to **`2`**.
   * **Memory (RAM):** Set the slider to **`12 GB`**.
5. **Networking:**
   * Ensure it is placed in a public subnet.
   * Under "Primary VNIC", select **Assign a public IPv4 address**.
6. **Add SSH Keys:**
   * Generate an SSH key on your local machine (if you haven't): `ssh-keygen -t ed25519 -f ~/.ssh/oracle_key`
   * Select **"Paste public keys"** in OCI and paste the contents of `~/.ssh/oracle_key.pub`.
7. **Boot Volume:**
   * Keep the default (usually 47GB or 50GB), or increase up to 200GB (which is the free tier max).
8. Click **Create** and wait for the instance to show a green **Running** status. Note the **Public IP Address**.

### 1.2 Configuring Oracle VCN Ingress Rules (Firewall)
By default, Oracle blocks everything except SSH (port 22). To communicate with your bot (if you decide to use webhooks instead of polling), you need to open port 443. For now, we will open port 8000 for standard API testing if needed.
1. In the OCI Console, go to your Instance details. 
2. Click on the attached **Subnet** name.
3. Click on the **Default Security List**.
4. Click **Add Ingress Rules**:
   * **Source CIDR:** `0.0.0.0/0`
   * **IP Protocol:** TCP
   * **Destination Port Range:** `8000` (Add more rules for `443` or `80` if needed later).
   * Click **Add Ingress Rules**.

---

## 🛠️ Phase 2: Server Initialization & Dependency Installation

Connect to your new server from your local terminal:
```bash
ssh -i ~/.ssh/oracle_key ubuntu@<YOUR_VM_PUBLIC_IP>
```

### 2.1 Update & Upgrade OS packages
First, ensure the Ubuntu ARM environment is fully up-to-date.
```bash
sudo apt update && sudo apt upgrade -y
```

### 2.2 Open the OS Firewall (Iptables/UFW)
Even though we opened ports in the OCI console, the Ubuntu image has its own internal `iptables` rules that block traffic.
```bash
# Allow port 8000 through the local iptables
sudo iptables -I INPUT -p tcp -m tcp --dport 8000 -j ACCEPT
sudo iptables -I INPUT -p tcp -m tcp --dport 443 -j ACCEPT

# Save the rules so they persist across reboots
sudo apt install -y netfilter-persistent iptables-persistent
sudo netfilter-persistent save
```

### 2.3 Install Core Development Tools
```bash
sudo apt install -y curl wget git build-essential tmux htop python3-venv python3-pip sqlite3
```

---

## 🧠 Phase 3: The LLM Engine (Ollama + Qwen3 8B)

For a 12GB RAM ARM CPU, **Qwen3:8b** is an exceptional choice. It is highly capable at coding, reasoning, and following system prompts. 

To fit an 8 Billion parameter model into 12GB of RAM alongside the OS and context window (KV cache), we must use **Quantization** (compressing the model weights from 16-bit to 4-bit).

### 3.1 Install Ollama
Ollama natively supports ARM (`aarch64`) and uses `llama.cpp` under the hood, which is highly optimized for Apple Silicon and ARM CPUs.
```bash
curl -fsSL https://ollama.com/install.sh | sh
```
Verify the service is running:
```bash
sudo systemctl status ollama
# Press 'q' to exit the status log
```

### 3.2 Download and Tune the Qwen3 8B Model
We will pull the precise quantized version of the model. For an ARM CPU with 12GB RAM, the **`q4_K_M`** (4-bit quantization) is the sweet spot between speed and intelligence. The model file is about **5.2 GB**, leaving roughly 6GB free for the OS, your Python app, and the LLM's context memory.

```bash
ollama run qwen2.5:7b-instruct-q4_K_M
```
*(Note: At the time of this writing, Qwen 2.5 is the latest stable high-performance iteration widely available on Ollama in 8B/7B class that dominates CPU inference. If `qwen3` is explicitly available in your Ollama registry today, replace with `qwen3:8b`).*

Wait for the download to finish. Once you see the `>>>` prompt, you can chat with it to test the CPU inference speed. Type `/bye` to exit.

---

## 🤖 Phase 4: Building Valentine's Mind (Python Framework)

We need a Python brain that connects to Telegram (or Discord) and talks to Ollama for the thinking process.

### 4.1 Set up the Virtual Environment
```bash
mkdir -p ~/valentine
cd ~/valentine
python3 -m venv venv
source venv/bin/activate
```

### 4.2 Install Python Dependencies
```bash
# We use LangChain for the agent logic and python-telegram-bot for the interface
pip install langchain langchain-community langchain-core pydantic python-telegram-bot "httpx[http2]"
```

### 4.3 Create `agent.py`
Create a file to house Valentine's logic.
```bash
nano ~/valentine/agent.py
```

Paste the following master script. **You must replace `YOUR_TELEGRAM_BOT_TOKEN`** with a token from the Telegram `BotFather` (search for @BotFather on Telegram, send `/newbot`, and follow instructions).

```python
import os
import logging
from langchain_community.llms import Ollama
from langchain_core.prompts import PromptTemplate
from langchain.memory import ConversationBufferWindowMemory
from langchain.chains import LLMChain
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Logging setup
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Valentine's Configuration ---
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN" # Replace this!
MODEL_NAME = "qwen2.5:7b-instruct-q4_K_M" # Or "qwen3:8b" if pulled

# Initialize the Ollama LLM Connection
llm = Ollama(
    model=MODEL_NAME,
    num_ctx=4096, # Context window size. 4K fits nicely in 12GB RAM.
    num_thread=2, # Restrict to 2 OCPUs to prevent OS starvation on Ampere A1.
)

# System Prompt defining the Persona
SYSTEM_PROMPT = """You are Valentine, a calm, highly capable, and extremely intelligent personal AI assistant. 
You are speaking to your creator. Be concise, witty, and deeply helpful. Do not use overly enthusiastic language. 
Provide direct answers without unnecessary pleasantries unless specifically asked.

Previous Conversation Logs:
{chat_history}

User: {human_input}
Valentine:"""

prompt = PromptTemplate(input_variables=["chat_history", "human_input"], template=SYSTEM_PROMPT)

# Memory: Keep only the last 10 interactions to save RAM and context window limits.
memory = ConversationBufferWindowMemory(k=10, memory_key="chat_history")
agent_chain = LLMChain(llm=llm, prompt=prompt, memory=memory, verbose=False)

# --- Telegram Bot Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers the /start command in Telegram."""
    memory.clear() # Reset memory on start
    await update.message.reply_text("Valentine systems online. Memory initialized. How may I assist you?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text messages from the user."""
    user_text = update.message.text
    logger.info(f"User: {user_text}")
    
    # Send a "typing..." indicator to Telegram while the ARM CPU processes the prompt
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    try:
        # Generate response using Qwen model via Ollama
        response = agent_chain.predict(human_input=user_text)
        logger.info(f"Valentine: {response}")
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Error during LLM inference: {e}")
        await update.message.reply_text("Neural pathways disrupted. Please try again.")

def main():
    """Start the bot."""
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("ERROR: Please set your Telegram bot token in agent.py")
        return

    # Build the Application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start polling Telegram for messages
    print("Valentine Bot is starting and connecting to Telegram network (Polling Mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
```
*(Press `Ctrl+O`, `Enter`, `Ctrl+X` to save and exit nano).*

---

## 🚀 Phase 5: Daemonization (Keeping Valentine Online 24/7)

If you just run `python agent.py`, the bot will die when you close your SSH terminal. We will set up a **Systemd Service** so the bot runs forever in the background and starts automatically if the Oracle server reboots.

### 5.1 Create the Service File
```bash
sudo nano /etc/systemd/system/valentine.service
```

### 5.2 Paste the following configuration:
*Make sure to change `ubuntu` if you are using a different username.*
```ini
[Unit]
Description=Valentine AI Telegram Bot
After=network.target ollama.service

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/valentine
# Use the python executable from inside the virtual environment
ExecStart=/home/ubuntu/valentine/venv/bin/python /home/ubuntu/valentine/agent.py
Restart=always
RestartSec=10
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```
*(Press `Ctrl+O`, `Enter`, `Ctrl+X` to save and exit nano).*

### 5.3 Enable and Start the Agent Service
```bash
# Reload systemd to recognize the new file
sudo systemctl daemon-reload

# Enable it to start on boot
sudo systemctl enable valentine.service

# Start it immediately
sudo systemctl start valentine.service
```

### 5.4 Monitor the Logs
To ensure Valentine is running correctly and to watch the thinking process:
```bash
sudo journalctl -u valentine.service -f
```

---

## 📊 Phase 6: Performance Tuning & Troubleshooting

**Monitoring Resource Usage:**
Open a separate SSH window and run `htop`.
When you message Valentine on Telegram, watch the CPU bars. Because we set `num_thread=2` in PyTorch/Ollama, the inference should max out exactly 2 OCPUs, allowing the bot to generate text at roughly **5 to 10 tokens per second**. This is highly readable and very fast for a free CPU tier.

**RAM Limits:**
If Valentine crashes mid-sentence, the OOM (Out Of Memory) killer might be stopping Ollama. Run:
```bash
sudo dmesg -T | grep -i oom
```
If you see Ollama being killed:
1. Ensure you are using the `q4_K_M` quantization. Do not use an unquantized model.
2. Reduce the `num_ctx` in `agent.py` from `4096` to `2048`.

---
✅ **End of Setup.**
Valentine is now fully self-sufficient on your Oracle Cloud VM. Open Telegram and send `/start` to your Bot to begin interaction.
