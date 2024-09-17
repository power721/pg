#!/usr/bin/env bash

PACKAGE=$(ls -t ~/Downloads/Telegram\ Desktop/pg.*.zip | tail -1)
cp "$PACKAGE" pg.zip
ls "$PACKAGE" | awk -F/ '{print $NF}' | tee >version.txt
