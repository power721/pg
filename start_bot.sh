echo "git pull"
git pull origin main

PID=$(ps aux | grep bot.py | grep -v chat_bot.py | grep -v grep | awk '{print $2}')
echo "kill $PID"
kill $PID
nohup python bot.py > bot.log 2>&1 &
echo "bot started"
