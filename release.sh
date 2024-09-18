#!/usr/bin/env bash

old_version=$(cat version.txt)
PACKAGE=$(ls -t ~/Downloads/Telegram\ Desktop/pg.*.zip | head -1)
cp "$PACKAGE" pg.zip
new_version=$(ls "$PACKAGE" | awk -F/ '{print $NF}' | awk -F. '{print $2}')
echo "old version: ${old_version}  new version: ${new_version}"

if [ "${old_version}" != "${new_version}" ]; then
  echo "${new_version}" >version.txt
  cat "commit"

  git commit -am "update $(cat version.txt)"
  git push
fi
