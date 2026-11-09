#!/bin/sh
# Optional: the plugin clones AFC itself at Klipper start. This does it by
# hand: clone AFC-Klipper-Add-On into this plugin folder at the tested commit and
# create AFC's config folder. Run as the klipper user (Kiln: plugins and
# printer_data belong to klipper):
#   su -s /bin/sh klipper -c "sh /data/ad5x/plugins/afc/install.sh"
set -e
cd "$(dirname "$0")"
AFC_REPO=https://github.com/AFCProject/AFC-Klipper-Add-On.git
AFC_COMMIT=$(cat AFC_COMMIT)
if [ ! -d AFC-Klipper-Add-On/.git ]; then
    git clone --quiet "$AFC_REPO" AFC-Klipper-Add-On
fi
git -C AFC-Klipper-Add-On remote set-url origin "$AFC_REPO"
git -C AFC-Klipper-Add-On fetch --quiet origin
git -C AFC-Klipper-Add-On checkout --quiet "$AFC_COMMIT"
mkdir -p /home/klipper/printer_data/config/AFC
echo "AFC $(git -C AFC-Klipper-Add-On rev-parse --short HEAD) ready; restart Klipper"
