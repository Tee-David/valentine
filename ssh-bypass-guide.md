## Bypassing Oracle SSH MaxStartups (Bot Attack)

Your Oracle server is currently under a brute-force SSH attack from automated bots across the internet. Because so many bots are hitting port 22 simultaneously, the `sshd` service has reached its `MaxStartups` limit and is dropping all new connections—including ours.

To securely bypass this, we will use your local machine as a bridge.

### Step 1: Lock Down the Oracle Firewall
First, we must stop the bots from hitting Port 22 entirely.
1. Go to your Oracle Cloud Console -> **Virtual Cloud Networks**.
2. Click `valentines-vcn` -> **Security Lists** -> `Default Security List...`
3. Edit the existing Ingress Rule for **Port 22** that we changed to `0.0.0.0/0`.
4. Change the **Source CIDR** from `0.0.0.0/0` to exactly: `102.93.8.98/32`
   *(This ensures ONLY your local internet connection is allowed to talk to Port 22 on the Oracle VM).*
5. Click **Save Rules**.

### Step 2: Establish the SSH Connection Locally
Once the firewall rule is updated, the bot traffic will be blocked instantly, and the `MaxStartups` queue will clear out within 60 seconds.

Open the terminal on your *local machine* and run this command. This will connect YOU directly to the server:
```bash
ssh -i /path/to/your/valentines-private.key ubuntu@<YOUR_VM_PUBLIC_IP>
```
*(If the key is on your desktop like earlier, use: `ssh -i /home/teedavid/Desktop/Projects/valentine/valentines-private.key ubuntu@<YOUR_VM_PUBLIC_IP>`)*

### Step 3: Change the SSH Port (Optional but Highly Recommended)
Once you are successfully logged in via your local terminal, the best long-term fix is to move SSH off of Port 22 so bots can't find it easily. 
While logged into the Ubuntu VM, run:
```bash
sudo sed -i 's/#Port 22/Port 2222/g' /etc/ssh/sshd_config
sudo systemctl restart sshd
```
If you do this, remember to also add an Ingress Rule in Oracle for TCP Port `2222` from `0.0.0.0/0`, and then you can connect from anywhere using:
`ssh -p 2222 -i ... ubuntu@<IP>`

### Once You Are In:
Please let me know once you have successfully opened a terminal session on the VM, and I will give you the exact commands to paste in to continue the Valentine setup process!
