"""flight-price-predictor の構成図（Python diagrams）。

    pip install diagrams      # Graphviz（dot）も要る: brew install graphviz
    python3 docs/diagrams/architecture.py
      → docs/diagrams/architecture.png

**ラベルは英語。** コードから読み取れる構成だけを描く。 秘密の値（鍵・トークン）は載せない。
"""
import pathlib

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.storage import S3
from diagrams.generic.database import SQL
from diagrams.onprem.client import Client, User, Users
from diagrams.onprem.network import Caddy
from diagrams.onprem.vcs import Github
from diagrams.programming.framework import FastAPI
from diagrams.programming.language import Python
from diagrams.generic.network import Firewall
from diagrams.onprem.compute import Server

HERE = pathlib.Path(__file__).resolve().parent
FONT = "Helvetica"

graph = {"fontname": FONT, "fontsize": "20", "pad": "0.4",
         "nodesep": "0.7", "ranksep": "1.0", "splines": "spline"}
node = {"fontname": FONT, "fontsize": "12"}
edge = {"fontname": FONT, "fontsize": "10"}

with Diagram("flight-price-predictor architecture", show=False,
             filename=str(HERE / "architecture"), outformat="png",
             direction="TB", graph_attr=graph, node_attr=node, edge_attr=edge):

    users = Users("Users\n(browser / PWA)")

    with Cluster("External services", graph_attr={"fontname": FONT}):
        gflights = Server("Google Flights\n(via fast-flights)")
        fx = Server("Frankfurter\nFX rates")
        eia = Server("EIA oil prices\n(disabled: coefficient 0)")
        push = Server("Web Push services\n(FCM / Apple / Mozilla)")

    with Cluster("AWS us-west-2", graph_attr={"fontname": FONT}):
        with Cluster("Lightsail instance  flights.suosolutions.com",
                     graph_attr={"fontname": FONT}):
            fw = Firewall("Lightsail firewall\n80/443\n(22 only during maintenance)")
            caddy = Caddy("Caddy\nHTTPS (Let's Encrypt)\noverwrites X-Forwarded-For")
            api = FastAPI("FastAPI / uvicorn\n127.0.0.1:8000\nUI, API, /api/ops")
            with Cluster("Daily job (APScheduler 03:00 PT)",
                         graph_attr={"fontname": FONT}):
                job = Python("collect → predict → log verdicts\n→ FX → accuracy → notify\n→ KPI → dashboard")
            db = SQL("SQLite\n/var/lib/fpp/flights.db")

        with Cluster("S3", graph_attr={"fontname": FONT}):
            kpi = S3("KPI CSV")
            dash = S3("Dashboard\nJSON / HTML (ja, en)")

    with Cluster("Operations", graph_attr={"fontname": FONT}):
        dev = User("Developer (Mac)")
        repo = Github("GitHub\neqj619/flight-price-predictor")
        routine = Client("Claude cloud routine\n08:00 PT")
        artifact = Client("Claude artifacts\nops dashboard (ja, en)")

    # 利用者の経路
    users >> Edge(label="HTTPS") >> fw >> caddy >> api
    api >> db
    push >> Edge(label="price-drop alerts", style="dashed", constraint="false") >> users

    # 日次ジョブ
    job >> db
    job >> Edge(label="fares") >> gflights
    job >> fx
    job >> Edge(style="dotted", color="gray") >> eia
    job >> Edge(label="VAPID") >> push
    job >> Edge(label="boto3") >> kpi
    job >> dash

    # ダッシュボードの公開
    routine >> Edge(label="fetch", constraint="false") >> dash
    routine >> Edge(label="build & publish") >> artifact

    # 開発・配備
    dev >> Edge(label="git push") >> repo
    repo >> Edge(label="generator code", style="dashed") >> routine
    dev >> Edge(label="deploy.sh (rsync)\nssh-access.sh opens/closes 22",
                color="firebrick", constraint="false") >> fw
