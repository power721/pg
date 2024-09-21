#!/usr/bin/env bash

old_version=$(cat version.txt)
PACKAGE=$(ls -t ~/Downloads/Telegram\ Desktop/pg.*.zip | head -1)
mv "$PACKAGE" pg.zip
new_version=$(echo "$PACKAGE" | awk -F/ '{print $NF}' | awk -F. '{print $2}')
if [ "${new_version}" = "" ]; then
  exit
fi
echo "old version: ${old_version}  new version: ${new_version}"

if [ "${old_version}" != "${new_version}" ]; then
  echo "${new_version}" >version.txt
  echo "commit file"

  git commit -am "update $(cat version.txt)"
  git push origin main
  git push lab main
fi
