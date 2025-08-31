# main.py
import os
import io
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from botocore.exceptions import NoCredentialsError, ClientError
import boto3
from google.cloud import storage
import time
from botocore.client import Config
from google.api_core.client_options import ClientOptions


# Load environment variables (for local dev)
from dotenv import load_dotenv
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Multi-Cloud Replication Service")

class ReplicationRequest(BaseModel):
    s3_bucket: str
    s3_key: str

# Configuration from environment variables
try:
    GCS_TARGET_BUCKET = os.environ['GCS_TARGET_BUCKET']
    
    # Check for fake GCS server configuration
    
    # Boto3 will automatically use AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
    s3_client = boto3.client(
        's3', endpoint_url="http://localhost:4566",
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4'),
        region_name = 'us-east-1'
    )

    os.environ["STORAGE_EMULATOR_HOST"] = "http://localhost:4443"
    gcs_client = storage.Client(project="fake-project")
    logger.info(f"gcs_client: {gcs_client}")

    # Configure Google Cloud Storage Client for fake server or real GCP
    # if gcs_api_endpoint:
    #     logger.info(f"Using fake GCS server at {gcs_api_endpoint}")
    #     client_options = ClientOptions(api_endpoint=gcs_api_endpoint)
    #     gcs_client = storage.Client(client_options=client_options)
    # else:
    #     logger.info("Using real GCP endpoint.")
    #     # For real GCP, the client handles authentication via GOOGLE_APPLICATION_CREDENTIALS
    #     gcs_client = storage.Client()

    gcs_bucket = gcs_client.bucket(GCS_TARGET_BUCKET)
    logger.info(f"gcs_bucket: {gcs_bucket}")

except KeyError as e:
    logger.error(f"Missing required environment variable: {e}")
    raise RuntimeError(f"Configuration error: Missing environment variable {e}")

# Simple retry decorator
def retry_on_error(max_retries=3, delay_secs=2):
    def decorator(func):
        def wrapper(request: ReplicationRequest):
            retries = 0
            while retries < max_retries:
                try:
                    return func(request)
                except (ClientError, Exception) as e:
                    logger.warning(f"Attempt {retries + 1}/{max_retries} failed with error: {e}")
                    retries += 1
                    if retries < max_retries:
                        time.sleep(delay_secs)
                    else:
                        logger.error(f"Function {func.__name__} failed after {max_retries} attempts.")
                        raise
        return wrapper
    return decorator

@app.post("/v1/replicate")
@retry_on_error(max_retries=5)
def replicate_data(request: ReplicationRequest):
    """
    Handles the replication of a file from AWS S3 to Google Cloud Storage.
    """
    logger.info(f"$"*100)
    s3_bucket = request.s3_bucket
    s3_key = request.s3_key
    gcs_key = s3_key # Using the same key for simplicity
    logger.info(f"*"*100)

    # Idempotency Check: Does the file already exist in GCS?
    gcs_blob = gcs_bucket.blob(gcs_key)
    logger.info(f"gcs_blob: {gcs_blob}")
    if gcs_blob.exists():
        logger.info(f"File '{gcs_key}' already exists in GCS. Skipping replication.")
        return {"status": "skipped", "message": "File already exists in destination.", "s3_key": s3_key}

    logger.info(f"Initiating replication for s3://{s3_bucket}/{s3_key} to gs://{GCS_TARGET_BUCKET}/{gcs_key}")

    # Use a streaming approach with a temporary in-memory buffer
    try:
        s3_object_stream = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)['Body']
        
        # Upload the stream to GCS
        # Note: Boto3 provides a streamable body. `gcs_blob.upload_from_file` can take a file-like object.
        gcs_blob.upload_from_file(s3_object_stream)

        logger.info(f"Successfully replicated s3://{s3_bucket}/{s3_key} to gs://{GCS_TARGET_BUCKET}/{gcs_key}")
        return {"status": "success", "message": "Replication complete.", "s3_key": s3_key}

    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise HTTPException(status_code=404, detail=f"S3 object not found: s3://{s3_bucket}/{s3_key}")
        else:
            logger.error(f"S3 error during replication: {e}")
            raise HTTPException(status_code=500, detail="An error occurred during S3 operation.")
    except Exception as e:
        logger.error(f"Unexpected error during replication: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")