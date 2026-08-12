cd /home
apt install -y python3-full python3-venv python3-pip
python3 -m venv venv
/home/venv/bin/pip install --upgrade pip
/home/venv/bin/pip install flask
/home/venv/bin/python -c "import flask; print(flask.__version__)"

/home/venv/bin/python app.py
