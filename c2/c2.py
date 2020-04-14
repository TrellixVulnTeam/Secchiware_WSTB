import hmac
import json
import os
import requests as rq
import signatures
import shutil
import sqlite3
import tempfile
import test_utils

from base64 import b64encode
from custom_collections import OrderedListOfDict
from datetime import datetime
from flask import abort, Flask, g, jsonify, request, Response
from flask_cors import CORS
from hashlib import sha256


def client_key_recoverer(key_id):
    return config['CLIENT_SECRET'] if key_id == "Client" else None

def node_key_recoverer(key_id):
    return config['NODE_SECRET'] if key_id == "Node" else None

def check_registered(ip, port):
    """Verifies if the given ip and port correspond to an active environment.

    If there is no match, it aborts the current request handler.

    Parameters
    ----------
    ip
        The ip to look for.
    port
        The port associated to the given ip to look for.
    """

    if not (ip in environments and port in environments[ip]):
        abort(404,
            description=f"No environment registered for {ip}:{port}")

def check_is_json():
    """Verifies that the current request's MIME type is 'application/json'.

    If that's not the case, it aborts the current request handler.
    """

    if not request.is_json:
        abort(415, description="Content Type is not application/json")

def check_digest_header():
    if not 'Digest' in request.headers:
        abort(400, description="'Digest' header mandatory.")
    if not request.headers['Digest'].startswith("sha-256="):
        abort(400, description="Digest algorithm should be sha-256.")
    digest = b64encode(sha256(request.get_data()).digest()).decode()
    if digest != request.headers['Digest'].split("=", 1)[1]:
        abort(400, description="Given digest does not match content.")

def check_authorization_header(key_recoverer, *mandatory_headers):
    if not 'Authorization' in request.headers:
        abort(401, description="No 'Authorization' header found in request.")
    try:
        is_valid = signatures.verify_authorization_header(
            request.headers['Authorization'],
            key_recoverer,
            lambda h: request.headers.get(h),
            request.method,
            request.path,
            request.query_string.decode(),
            mandatory_headers)
    except ValueError as e:
        abort(401, description=str(e))
    except Exception as e:
        abort(401, description="Invalid 'Authorization' header.")
    if not is_valid:
        abort(401, description="Invalid signature.")


app = Flask(__name__)
CORS(app, resources={
    r"/": {},
    r"/environments": {'methods': "GET"},
    r"/environments/[^/]+/[^/]+/+": {},
    r"/test_sets/*": {}
})


@app.errorhandler(400)
def bad_request(e):
    return jsonify(error=str(e)), 400

@app.errorhandler(401)
def unauthorized(e):
    res = jsonify(error=str(e))
    res.status_code = 401
    res.headers['WWW-Authenticate'] = 'SECCHIWARE-HMAC-256 realm="Access to C2"'
    return res

@app.errorhandler(404)
def not_found(e):
    return jsonify(error=str(e)), 404

@app.errorhandler(415)
def unsupported_media_type(e):
    return jsonify(error=str(e)), 415

@app.errorhandler(500)
def internal_server_error(e):
    return jsonify(error=str(e)), 500

@app.errorhandler(502)
def bad_gateway(e):
    return jsonify(error=str(e)), 502

@app.errorhandler(504)
def gateway_timeout(e):
    return jsonify(error=str(e)), 504


@app.route("/", methods=["GET"])
def is_up():
    return jsonify(success=True)

@app.route("/environments", methods=["GET"])
def list_environments():
    return jsonify(environments)

@app.route("/environments", methods=["POST"])
def add_environment():
    check_digest_header()
    check_authorization_header(node_key_recoverer, "Digest")
    check_is_json()
    if not ('ip' in request.json
            and 'port' in request.json
            and 'platform_info' in request.json):
        abort(400, description="One or more keys missing in request's body")

    ip = request.json['ip']
    port = request.json['port']
    platform_info = request.json['platform_info']

    to_insert = (
            ip,
            int(port),
            platform_info['platform'],
            platform_info['node'],
            platform_info['os']['system'],
            platform_info['os']['release'],
            platform_info['os']['version'],
            platform_info['hardware']['machine'],
            platform_info['hardware']['processor'],
            platform_info['python']['build'][0],
            platform_info['python']['build'][1],
            platform_info['python']['compiler'],
            platform_info['python']['implementation'],
            platform_info['python']['version']
    )
    db = get_database()
    cursor = db.execute(
        "INSERT INTO session "\
        "(env_ip, env_port, env_platform, env_node, env_os_system, "\
        "env_os_release, env_os_version, env_hw_machine, "\
        "env_hw_processor, env_py_build_no, env_py_build_date, "\
        "env_py_compiler, env_py_implementation, env_py_version) "\
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        to_insert)
    
    db.commit()

    if not ip in environments:
        environments[ip] = {}
    environments[ip][port] = {
        'session_id': cursor.lastrowid,
        'session_start': cursor.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', session_start, 'unixepoch') "\
            "FROM session WHERE id_session = ?",
            (cursor.lastrowid,)).fetchone()[0]
    }

    return Response(status=204, mimetype="application/json")

@app.route("/environments/<ip>/<port>", methods=["DELETE"])
def remove_environment(ip, port):
    global environments

    check_authorization_header(node_key_recoverer)
    check_registered(ip, port)
    
    del environments[ip][port]
    if not environments[ip]:
        del environments[ip]
    
    return Response(status=204, mimetype="application/json")

@app.route("/environments/<ip>/<port>/info", methods=["GET"])
def get_environment_info(ip, port):
    check_registered(ip, port)
    
    try:
        resp = rq.get(f"http://{ip}:{port}/info")
    except rq.exceptions.ConnectionError:
        abort(504,
            description="The requested environment could not be reached")

    if resp.status_code == 200:
        return jsonify(resp.json())
    abort(502, description=f"Unexpected response from node at {ip}:{port}")
    
@app.route("/environments/<ip>/<port>/installed", methods=["GET"])
def list_installed_test_sets(ip, port):
    check_registered(ip, port)

    try:
        resp = rq.get(f"http://{ip}:{port}/test_sets")
    except rq.exceptions.ConnectionError:
        abort(504,
            description="The requested environment could not be reached")

    if resp.status_code == 200:
        return jsonify(resp.json())
    abort(502, description=f"Unexpected response from node at {ip}:{port}")

@app.route("/environments/<ip>/<port>/installed", methods=["PATCH"])
def install_packages(ip, port):
    check_digest_header()
    check_authorization_header(client_key_recoverer, "Digest")
    check_registered(ip, port)
    check_is_json()

    packages = request.json
    try:
        with tempfile.SpooledTemporaryFile() as f:
            # Can throw ValueError.
            test_utils.compress_test_packages(f, packages, TESTS_PATH)
            f.seek(0)
            prepared = rq.Request(
                "PATCH",
                f"http://{ip}:{port}/test_sets",
                files={'packages': f}).prepare()
        
        digest = b64encode(sha256(prepared.body).digest()).decode()
        prepared.headers['Digest'] = f"sha-256={digest}"

        headers = ['Digest']
        signature = signatures.new_signature(
            config['NODE_SECRET'],
            "PATCH",
            "/test_sets",
            signature_headers=headers,
            header_recoverer=lambda h: prepared.headers.get(h))
        prepared.headers['Authorization'] =\
            signatures.new_authorization_header("C2", signature, headers)

        resp = rq.Session().send(prepared)
    except ValueError as e:
        abort(400, description=str(e))
    except rq.exceptions.ConnectionError:
        abort(504,
            description="The requested environment could not be reached")
    
    if resp.status_code == 204:
        return Response(status=204, mimetype="application/json")
    if resp.status_code in {400, 401, 415}:
        abort(500,
            description="Something went wrong when handling the request")
    abort(502, description=f"Unexpected response from node at {ip}:{port}")

@app.route("/environments/<ip>/<port>/installed/<package>", methods=["DELETE"])
def delete_installed_package(ip, port, package):
    check_authorization_header(client_key_recoverer)
    check_registered(ip, port)

    signature = signatures.new_signature(
        config['NODE_SECRET'],
        "DELETE",
        f"/test_sets/{package}")
    authorization_content = signatures.new_authorization_header("C2", signature)

    try:
        resp = rq.delete(
            f"http://{ip}:{port}/test_sets/{package}",
            headers={'Authorization': authorization_content})
    except rq.exceptions.ConnectionError:
        abort(504,
            description="The requested environment could not be reached")

    if resp.status_code == 204:
        return Response(status=204, mimetype="application/json")
    if resp.status_code in {401, 404}:
        return abort(404, description=f"'{package}' not found at {ip}:{port}")
    abort(502, description=f"Unexpected response from node at {ip}:{port}")

@app.route("/environments/<ip>/<port>/report", methods=["GET"])
def execute_tests(ip, port):
    check_registered(ip, port)
    
    url = f"http://{ip}:{port}/report"
    if request.args:
        valid_keys = {'packages', 'modules', 'test_sets'}
        difference = set(request.args.keys()) - valid_keys
        if difference:
            abort(400, f"Invalid keys {difference} found in query parameters")
        else:
            url += f"?{request.query_string.decode()}"

    try:
        resp = rq.get(url)
    except rq.exceptions.ConnectionError:
        abort(504,
            description="The requested environment could not be reached")

    if resp.status_code == 200:
        return jsonify(resp.json())
    if resp.status_code == 400:
        abort(500,
            description="Something went wrong when handling the request")
    abort(502, description=f"Unexpected response from node at {ip}:{port}")

@app.route("/test_sets", methods=["GET"])
def list_available_test_sets():
    return jsonify(available.content)

@app.route("/test_sets", methods=["PATCH"])
def upload_test_sets():
    global available

    if not request.mimetype == 'multipart/form-data':
        abort(415, description="Invalid request's content type")
    check_digest_header()
    if not (request.files and 'packages' in request.files):
        abort(400, description="'packages' key not found in request's body")
    check_authorization_header(client_key_recoverer, "Digest")
    
    try:
        new_packages = test_utils.uncompress_test_packages(
            request.files['packages'],
            TESTS_PATH)
    except Exception as e:
        print(str(e))
        abort(400, description="Invalid file content")

    new_info = []
    for new_pack in new_packages:
        new_pack = f"test_sets.{new_pack}"
        # If it is a new version, the next sentence removes the old one
        test_utils.clean_package(new_pack)
        new_info.append(
            test_utils.get_installed_package(new_pack))
    available.batch_insert(new_info)
    return Response(status=204, mimetype="application/json")

@app.route("/test_sets/<package>", methods=["DELETE"])
def delete_package(package):
    global available

    check_authorization_header(client_key_recoverer)

    package_path = os.path.join(TESTS_PATH, package)
    if not os.path.isdir(package_path):
        abort(404, description=f"Package '{package}' not found")

    shutil.rmtree(package_path)
    available.delete(package)
    return Response(status=204, mimetype="application/json")


def init_database():
    db = sqlite3.connect(DATABASE_PATH)
    cursor = db.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS session ("\
        "id_session INTEGER PRIMARY KEY,"\
        "session_start INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),"\
        "session_end INTEGER,"\
        "env_ip TEXT NOT NULL,"\
        "env_port INTEGER NOT NULL,"\
        "env_platform TEXT NOT NULL,"\
        "env_node TEXT NOT NULL,"\
        "env_os_system TEXT NOT NULL,"\
        "env_os_release TEXT NOT NULL,"\
        "env_os_version TEXT NOT NULL,"\
        "env_hw_machine TEXT NOT NULL,"\
        "env_hw_processor TEXT NOT NULL,"\
        "env_py_build_no TEXT NOT NULL,"\
        "env_py_build_date TEXT NOT NULL,"\
        "env_py_compiler TEXT NOT NULL,"\
        "env_py_implementation TEXT NOT NULL,"\
        "env_py_version TEXT NOT NULL)")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS execution ("\
        "id_execution INTEGER PRIMARY KEY,"\
        "fk_session INTEGER NOT NULL,"\
        "timestamp_registered INTEGER DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),"\
        "FOREIGN KEY (fk_session) REFERENCES session(id_session))")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS report ("\
        "id_report INTEGER PRIMARY KEY,"\
        "fk_execution INTEGER NOT NULL,"\
        "timestamp_start REAL NOT NULL,"\
        "timestamp_end REAL NOT NULL,"\
        "result INTEGER NOT NULL,"
        "additional_info TEXT,"\
        "FOREIGN KEY (fk_execution) REFERENCES execution(id_execution))")
    return db

def get_database():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = init_database()
    return db


SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__))
TESTS_PATH = os.path.join(SCRIPT_PATH, "test_sets")
DATABASE_PATH = os.path.join(SCRIPT_PATH, "secchiware.db")

with open(os.path.join(SCRIPT_PATH, "config.json"), "r") as config_file:
    config = json.load(config_file)
config['NODE_SECRET'] = config['NODE_SECRET'].encode()
config['CLIENT_SECRET'] = config['CLIENT_SECRET'].encode()

if not os.path.isdir(TESTS_PATH):
    os.mkdir(TESTS_PATH)
    open(os.path.join(TESTS_PATH, "__init__.py"), "w").close()

available = OrderedListOfDict('name', str)
try:
    available.content = test_utils.get_installed_test_sets("test_sets")
except Exception as e:
    print(str(e))
environments = {}

if __name__ == "__main__":
    app.run(host=config['IP'], port=config['PORT'])