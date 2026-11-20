# Thermal lab: environment gateway
Run with Python 3 and aiohttp. From the repository root:
~~~sh
python -m pip install -r environment/requirements.txt
python -m unittest discover -s environment/tests -v
python environment/env_gateway.py --host 127.0.0.1 --port 28984 --db environment/local.sqlite3
~~~
Local smoke check: GET /env/v1/health. Firmware submits POST /env/v1/measurements and /env/v1/status. Set ENV_SENSOR_TOKEN outside the repository when provisioning a protected deployment. Start with a new local database; device setup and production rollout require separate verification.
