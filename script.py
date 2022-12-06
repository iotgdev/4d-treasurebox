import requests
import os

from os import environ
from typing import List, Dict, Any, Generator, Literal
import json
import time
import uuid
import csv
import logging
from io import StringIO

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s, %(levelname)s, %(message)s", filename="4Dtreasurebox.log", filemode="w"
)


MASTER_KEY = os.environ["TD_MASTER_KEY"]
ACCESS_KEY = os.environ["TD_ACCESS_KEY"]
USERNAME = os.environ["FOURD_USERNAME"]
PASSWORD = os.environ["FOURD_PASSWORD"]
DB = os.environ["TD_DB_NAME"]
TABLE = os.environ["TD_TABLE"]
COLUMN = os.environ["TD_COLUMN"]
CHANNEL = os.environ["FOURD_CHANNEL"]
FOURD_REGION = os.environ["FOURD_REGION"]
TD_REGION = os.environ["TD_REGION"]
STATUS_TABLE = os.environ["TD_STATUS_TABLE"]
NEW_TABLE = os.environ["TD_NEW_TABLE"]

regions = {
    'US': 'api.treasuredata.com',
    'EU': 'api.eu01.treasuredata.com',
    'KR': '	api.ap02.treasuredata.com',
    'JP': 'api.treasuredata.co.jp'
}

class TreasureDataException(Exception):
    pass


class FourDException(Exception):
    pass


def split_list(original_list: List, chunk_length: int) -> Generator[List[Any], None, None]:
    """Split original list into n-sized chunks."""
    for index in range(0, len(original_list), chunk_length):
        yield original_list[index : index + chunk_length]  # noqa: E203


def create_table(database_name: str, table_name: str) -> None:
    """Creates a new table in a given Treasure Data database"""
    url = f"https://{regions[TD_REGION]}/v3/table/create/{database_name}/{table_name}"
    headers = {"Authorization": f"TD1 {MASTER_KEY}"}
    resp = requests.post(url=url, headers=headers)
    if resp.status_code != 200:
        raise TreasureDataException("Error creating new table in Treasure Data")


def set_timetable_schema(database_name: str, table_name: str) -> None:
    """Sets the schema of the timetable in Treasure Data"""
    schema = '[["status","string","status"],["filename","string","filename"]]'
    url = f"https://{regions[TD_REGION]}/v3/table/update/{database_name}/{table_name}?schema={schema}"
    headers = {"Authorization": f"TD1 {MASTER_KEY}"}
    resp = requests.post(url=url, headers=headers)
    if resp.status_code != 200:
        raise TreasureDataException("Error updating schema in Treasure Data")


def check_table_exists(table_name: str) -> bool:
    """Checks if a table exists in Treasure Data"""
    url = f"https://{regions[TD_REGION]}/v3/table/list/{DB}"
    headers = {"Authorization": f"TD1 {MASTER_KEY}"}
    resp = requests.get(url=url, headers=headers)
    if resp.status_code != 200:
        raise TreasureDataException("Error checking if table exists")
    tables = resp.json()["tables"]
    return next((table for table in tables if table["name"] == f"{table_name}"), None) is not None


def get_or_create_status_table(database_name: str, table_name: str) -> None:
    """Finds or creates a new/existing table in a given database in Treasure Data"""
    table_existence = check_table_exists(table_name)
    if not table_existence:
        create_table(database_name, table_name)
        set_timetable_schema(database_name, table_name)


def receipt(status: str, filename: str) -> None:
    """Inserts upload or last_run record to status table"""
    if status == "processed":
        delete_old_receipts = get_job_issue(f"DELETE FROM \"{STATUS_TABLE}\" WHERE filename='{filename}';")
        wait_for_result(delete_old_receipts)
    else:
        new_receipt = get_job_issue(f"INSERT INTO \"{STATUS_TABLE}\"(status, filename) VALUES ('{status}', '{filename}');")
        wait_for_result(new_receipt)


def last_upload_time() -> str:
    """Finds the time of the most recent upload"""
    job_id = get_job_issue(f"SELECT time FROM \"{STATUS_TABLE}\" WHERE status='last_run';")
    last_upload_time = wait_for_result(job_id)
    return last_upload_time


def get_files_to_download() -> List[str]:
    """Gets list of files pending download"""
    try:
        job_id = get_job_issue(f"SELECT filename FROM \"{STATUS_TABLE}\" where status='upload';")
        result = wait_for_result(job_id)
        return result.splitlines()
    except TreasureDataException as tde:
        logging.error(tde)
        return []


def get_job_issue(query: str) -> str:
    """Gets Treasure Data job id for SQL query"""
    url = f"https://{regions[TD_REGION]}/v3/job/issue/presto/{DB}"
    headers = {"Authorization": f"TD1 {MASTER_KEY}"}
    data = dict(query=query, Priority=0)
    resp = requests.post(url=url, data=data, headers=headers)
    if resp.status_code != 200:
        raise TreasureDataException("Error getting job id")
    return resp.json()["job_id"]


def wait_for_result(job_id: str) -> str:
    """Tracks Treasure Data SQL query status and returns result"""
    url = f"https://{regions[TD_REGION]}/v3/job/show/{job_id}"
    headers = {"Authorization": f"TD1 {MASTER_KEY}"}
    for i in range(180):
        time.sleep(5)
        resp = requests.get(url=url, headers=headers)
        if resp.status_code != 200:
            logging.warning("API Error")
            continue
        status = resp.json()["status"]
        if status == "success":
            return query_result(job_id)
        if status != "running":
            raise TreasureDataException(resp.json())
    raise TreasureDataException("Job not successful after 15 minutes")


def query_result(job_id: str) -> str:
    """Returns result of Treasure Data SQL query"""
    url = f"https://{regions[TD_REGION]}/v3/job/result/{job_id}"
    headers = {"Authorization": f"TD1 {MASTER_KEY}"}
    resp = requests.get(url=url, headers=headers)
    if resp.status_code != 200:
        raise TreasureDataException("Query failed")
    return resp.text


def authenticate_user() -> str:
    """Authenticates user with 4D"""
    url = "https://api.4d.silverbulletcloud.com/rest/authenticate/login/"
    headers = headers = {"Content-type": "application/json"}
    data = {"username": f"{USERNAME}", "password": f"{PASSWORD}"}
    resp = requests.post(url, data=json.dumps(data), headers=headers)
    if resp.status_code != 200:
        raise FourDException("Failed to authenticate 4D user")
    return resp.json()["token"]


def get_signed_url(auth_token: str, operation: Literal["upload", "download"], filename: str) -> str:
    """Generates 4D presigned URL for uploading and downloading files"""
    url = "https://treasuredata.4d.silverbulletcloud.com/access"

    data = {
        "region": FOURD_REGION,
        "action": operation,
        "channel": CHANNEL,
        "filenames": [
            f"{filename}",
        ],
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {auth_token}"}
    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code != 200:
        raise FourDException("Failed to generate presigned URL")
    return resp.json()["urls"][0]


def upload_to_4d(presigned_url: str, content: str, filename: str) -> None:
    """Uploads URLs to 4D"""
    resp = requests.put(url=presigned_url, data=content)
    if resp.status_code != 200:
        raise FourDException("Failed to upload URLs to 4D")
    receipt("upload", filename)


def get_4d_context_matches(presigned_url: str) -> str:
    """Downloads processed URLs from 4D"""
    resp = requests.get(presigned_url)
    if resp.status_code == 200:
        return resp.text
    elif resp.status_code == 404:
        return ""
    else:
        raise FourDException("Failed to download processed URLs from 4D")


def create_records(context_matches: str) -> None:
    """Takes 4D context matches and inserts them into new Treasure Data table"""
    f = StringIO(context_matches)
    csv_reader = csv.DictReader(f)
    for row in csv_reader:
        try:
            record_context_match(row)
        except TreasureDataException as tde:
            logging.error(tde)


def record_context_match(data: Dict[str, str]) -> None:
    """Inserts row into 4D enriched table"""
    url = f"https://in.treasuredata.com/postback/v3/event/{DB}/{NEW_TABLE}"
    headers = {
        "Content-Type": "application/json",
        "X-TD-Write-Key": f"{ACCESS_KEY}",
    }
    json_data = json.dumps(data)
    resp = requests.post(url=url, data=json_data, headers=headers)
    if resp.status_code != 200:
        raise TreasureDataException("Failed to insert record into 4D enriched Treasure Data table")


def get_urls_from_td() -> List[str]:
    """Collects new URLs if there are any"""
    last_upload = last_upload_time()
    if last_upload == "":
        job_id = get_job_issue(f"SELECT {COLUMN} FROM \"{TABLE}\"")
    else:
        job_id = get_job_issue(f"SELECT {COLUMN} FROM \"{TABLE}\" WHERE time >= {last_upload}")
    return wait_for_result(job_id).splitlines()


def set_last_run_time() -> None:
    """updates status table showing when the upload function was last run"""
    last_run_job_id = get_job_issue(f"DELETE FROM \"{STATUS_TABLE}\" WHERE status='last_run';")
    wait_for_result(last_run_job_id)
    receipt("last_run", "")


def four_d_upload(urls: List[str]) -> None:
    """Uploads URLs to 4D endpoint in batches"""
    auth_token = authenticate_user()
    for batch in split_list(urls, 100000):
        filename = str(uuid.uuid4())
        file_content = "\n".join(batch)
        upload_url = get_signed_url(auth_token, "upload", filename)
        upload_to_4d(upload_url, file_content, filename)


def four_d_download(filenames_to_download: List[str]) -> Generator[str, None, None]:
    """Downloads context matches from 4D endpoint"""
    auth_token = authenticate_user()
    for filename in filenames_to_download:
        download_url = get_signed_url(auth_token, "download", filename)
        context_matches = get_4d_context_matches(download_url)
        yield context_matches


def upload() -> None:
    """
    1. Checks for existence of status table
    2. Gets new URLs
    3. Uploads URLs to 4D endpoint
    """
    try:
        get_or_create_status_table(DB, STATUS_TABLE)
        urls = get_urls_from_td()
        if not urls:
            return
        set_last_run_time()
        four_d_upload(urls)
    except Exception:
        logging.exception("An error occurred")
        raise


def download() -> None:
    """
    1. Checks if URLs are ready to be downloaded
    2. Downloads processed URLs
    3. Inserts processed URLs into 4D enriched table
    """
    try:
        files_to_download = get_files_to_download()
        for filename, context_matches in zip(files_to_download, four_d_download(files_to_download)):
            if context_matches == "":
                continue
            create_records(context_matches)
            receipt("processed", filename)
    except Exception:
        logging.exception("An error occurred")
        raise