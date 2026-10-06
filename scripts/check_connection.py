import os
import pymysql
from dotenv import load_dotenv

load_dotenv()
conn = pymysql.connect(
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT")),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME"),
)
with conn.cursor() as cur:
    cur.execute("SELECT VERSION()")
    print("Connected! MySQL version:", cur.fetchone()[0])
conn.close()