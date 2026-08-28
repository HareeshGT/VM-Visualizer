#!/bin/bash
set -e

read -rp "Enter Docker tag (e.g. R258-11Aug): " TAG

if [ -z "$TAG" ]; then
    echo "Error: Tag cannot be empty."
    exit 1
fi

sudo git stash
sudo git pull --rebase origin dev
sudo git log --oneline | head -n 5
sudo git stash pop || true

sudo ./apiservice_docker.sh

# Uncomment if needed
sudo docker tag cowapiservice:1.1 066744120626.dkr.ecr.us-west-2.amazonaws.com/compliancecow/test/pcapiservice:$TAG
sudo docker push 066744120626.dkr.ecr.us-west-2.amazonaws.com/compliancecow/test/pcapiservice:$TAG
