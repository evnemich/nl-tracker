echo killing 
kill -9 $(pgrep -f ind_hag)
if [ $? -eq 0 ]; then
  echo killed
else
  echo no process found
fi

echo clearing cached data
rm -rf artifacts/
echo cleared

nohup python3 ind_hague.py monitor &
echo restarted
echo 
