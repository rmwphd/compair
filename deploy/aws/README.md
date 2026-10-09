# compair on AWS EC2 (free tier)

One `t3.micro` running Amazon Linux 2023 with Docker. Caddy serves HTTPS on a free `<ip>.sslip.io` hostname, so no domain is needed. The data is a SQLite file on the instance's disk.

**Cost.** `t3.micro` and up to 30 GB of disk are free-tier eligible. The public IPv4 address is billed at about $0.005/hour; this is covered by the free tier or free credits on most new accounts. The instance is launched with `CpuCredits=standard` so sustained CPU use is throttled rather than billed. **Terminate everything when you're done testing** (see Teardown below).

## 1. Create the resources (run from the repo root)

```bash
export AWS_REGION=us-east-1
MYIP=$(curl -s https://checkip.amazonaws.com)

# SSH key, saved locally only
aws ec2 create-key-pair --key-name compair --key-type ed25519 \
  --query KeyMaterial --output text > ~/.ssh/compair.pem && chmod 600 ~/.ssh/compair.pem

# Firewall: SSH from your current IP only; web from anywhere
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --group-name compair --description "compair web + SSH" \
  --vpc-id "$VPC" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "$MYIP/32"
aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
  'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]' \
  'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]'

# Latest Amazon Linux 2023 image + launch
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)
ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type t3.micro --key-name compair \
  --security-group-ids "$SG" --user-data file://deploy/aws/user-data.sh \
  --credit-specification CpuCredits=standard --metadata-options HttpTokens=required \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=16,VolumeType=gp3}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=compair}]' \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$ID"
IP=$(aws ec2 describe-instances --instance-ids "$ID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "instance $ID at $IP  ->  https://${IP//./-}.sslip.io"
```

## 2. Configure and start the app

Wait 1–2 minutes for `user-data.sh` to finish (it creates `/var/lib/cloud/compair-bootstrap-done`). Then:

```bash
ssh -i ~/.ssh/compair.pem ec2-user@$IP
cd /opt/compair
cp .env.example .env && nano .env
#   ADMIN_PASSWORD=<choose one>
#   SECRET_KEY=<python3 -c "import secrets; print(secrets.token_hex(32))">
#   SECURE_COOKIES=1
#   DATA_PATH=/opt/compair/data
#   SITE_ADDRESS=<ip-with-dashes>.sslip.io
docker build -t compair:latest .
docker compose --profile https up -d --no-build
docker compose logs -f caddy      # wait for "certificate obtained successfully"
```

The image is built with `docker build` because `docker compose build` needs the buildx plugin, which Amazon Linux doesn't ship. `--no-build` then reuses that image.

## Updating after a `git push`

```bash
ssh -i ~/.ssh/compair.pem ec2-user@$IP 'cd /opt/compair && git pull && docker build -t compair:latest . && docker compose --profile https up -d --no-build'
```

## Teardown

Download a backup first (Admin → **Backup DB**). Then:

```bash
aws ec2 terminate-instances --instance-ids "$ID" && aws ec2 wait instance-terminated --instance-ids "$ID"
aws ec2 delete-security-group --group-id "$SG"
aws ec2 delete-key-pair --key-name compair && rm ~/.ssh/compair.pem
```

If you lose the variables, look the resources up by name: the instance is tagged `Name=compair`, and the security group and key pair are both called `compair`.

## Notes

- **Changing IP.** Stopping and starting the instance gives it a new public IP, which breaks the sslip.io URL. Either keep it running, or attach an Elastic IP (also billed hourly once outside the free tier).
- **Your own domain.** Point an A record at the IP and set `SITE_ADDRESS=survey.example.com`.
- **SSH access.** SSH is open only to the IP you created the security group from. When you're on another network, add your new IP:
  ```bash
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "$(curl -s https://checkip.amazonaws.com)/32"
  ```
