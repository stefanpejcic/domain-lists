#!/bin/bash

# 1. download zone files
/home/venv/bin/python /home/download_zones.py

# 2. compare with previous date and geenerate lists
/home/venv/bin/python /home/process_zones.py

# 3. download tld info
#NO LONGER USED# /home/venv/bin/python /home/tld_scrapper.py

# todo: generate counters and distribution


# 4. cleanup (~9.6G)
rm -rf /home/downloads/

# 5. restart app
pkill -f '/home/venv/bin/python app.py' 2>/dev/null; /home/venv/bin/python app.py &
