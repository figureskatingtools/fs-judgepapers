import azure.functions as func
import logging
import os
import tempfile
import shutil
import base64
import json
import re
import io
from pypdf import PdfReader
from datetime import datetime
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from datetime import datetime, timedelta
from processor import process_judging_papers
from categories import load_categories, match_category, parse_filename_generic
from competition_schedule import parse_competition_schedule, get_schedule_start_time

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Maximum file upload size: 25 MB
MAX_UPLOAD_SIZE = 25 * 1024 * 1024

def is_user_allowed(email: str) -> bool:
    """
    Check if the user is allowed to perform sensitive operations.
    Policy for v1.0.0: All authenticated Entra ID users are allowed.
    To restrict access, implement an allowlist here (e.g. from Table Storage or env var).
    """
    return True

def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """
    Verify the request came from the Web App proxy by checking the shared
    secret header. The function endpoint is public, so this prevents anyone
    from spoofing the X-Forwarded-User-Email header directly.

    Enforced only when PROXY_SHARED_SECRET is set (so local dev and any
    brief pre-rollout window fail open rather than locking everyone out).
    """
    expected = os.environ.get("PROXY_SHARED_SECRET")
    if not expected:
        return True
    provided = req.headers.get("X-Proxy-Secret") or req.headers.get("x-proxy-secret")
    return provided == expected

def _decode_jwt_payload(token: str) -> dict | None:
    """Decode the payload of a JWT token without verification (base64 only)."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        # JWT base64url decode (add padding)
        payload_b64 = parts[1]
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as e:
        logging.error(f"Error decoding JWT payload: {e}")
        return None

def get_user_email_from_header(req: func.HttpRequest) -> str | None:
    """
    Extracts the user email from the X-MS-CLIENT-PRINCIPAL header 
    injected by Azure Static Web Apps or App Service Auth,
    or from the Authorization Bearer JWT token.
    """
    # 0. Reject requests that didn't come through the Web App proxy
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None

    # 1. Try Direct Header (Standard Easy Auth)
    val = req.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or req.headers.get("x-ms-client-principal-name")
    if val:
        logging.info(f"Authenticated as (Header Name): {val}")
        return val

    # 2. Try forwarded header from Web App proxy (server.js)
    forwarded = req.headers.get("X-Forwarded-User-Email") or req.headers.get("x-forwarded-user-email")
    if forwarded:
        logging.info(f"Authenticated as (Forwarded): {forwarded}")
        return forwarded

    # 3. Try Base64 Header (SWA / Advanced)
    header = req.headers.get("x-ms-client-principal") or req.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header:
        try:
            decoded = base64.b64decode(header).decode("utf-8")
            principal = json.loads(decoded)
            email = principal.get("userDetails")
            logging.info(f"Authenticated as (Base64): {email}")
            return email
        except Exception as e:
            logging.error(f"Error parsing auth header: {e}")

    # 3. Try Authorization Bearer token (JWT id_token or access_token from App Service Easy Auth)
    auth_header = req.headers.get("Authorization") or req.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        claims = _decode_jwt_payload(token)
        if claims:
            logging.info(f"JWT claims present: {list(claims.keys())}")
            # Try standard email claims (works for work accounts)
            email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or claims.get("unique_name")
            # Personal accounts (live.com/outlook.com) may use 'emails' array
            if not email:
                emails = claims.get("emails")
                if isinstance(emails, list) and emails:
                    email = emails[0]
            # Last resort: use 'name' or 'oid' as identity
            if not email:
                email = claims.get("name") or claims.get("oid")
            if email:
                logging.info(f"Authenticated as (Bearer JWT): {email}")
                return email
            else:
                logging.warning(f"JWT decoded but no usable identity claim found. Claims: {list(claims.keys())}")

    # DEBUG: Log if no identity found
    safe_headers = {k: v for k, v in req.headers.items() if 'auth' not in k.lower() and 'cookie' not in k.lower()}
    logging.warning(f"No Identity found. Safe Headers: {safe_headers}")
    return None

@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS)
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Checking user permission...')
    
    email = get_user_email_from_header(req)
    
    if not email:
        return func.HttpResponse(json.dumps({"allowed": False, "email": None}), mimetype="application/json", status_code=401)

    return func.HttpResponse(json.dumps({"allowed": True, "email": email}), mimetype="application/json")


def get_blob_service_client():
    """Helper to connect to Blob Storage"""
    try:
        # Managed Identity
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            account_url = f"https://{account_name}.blob.core.windows.net"
            return BlobServiceClient(account_url=account_url, credential=credential)
        
        # Connection String
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return BlobServiceClient.from_connection_string(connection_string)
            
        return None
    except Exception as e:
        logging.error(f"Failed to create blob client: {e}")
        return None

def get_table_client(table_name="generatedpapers"):
    """Helper to connect to Table Storage"""
    try:
        # Managed Identity
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            return TableClient(endpoint=endpoint, table_name=table_name, credential=credential)
        
        # Connection String
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return TableClient.from_connection_string(conn_str=connection_string, table_name=table_name)
            
        return None
    except Exception as e:
        logging.error(f"Failed to create table client: {e}")
        return None

def create_and_store_sas_link(blob_service_client, container_name, blob_name, competition, filename, file_size=0):
    try:
        table_client = get_table_client()
        if not table_client:
            logging.warning("No table client available, skipping SAS creation")
            return

        # Ensure table exists (idempotent usually, checking first saves errors)
        try:
             table_client.create_table()
        except:
             pass

        # Generate SAS
        start_time = datetime.utcnow()
        expiry = start_time + timedelta(days=5)
        sas_token = ""
        
        account_name = blob_service_client.account_name
        blob_url_base = f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}"
        
        # Managed Identity Logic
        if os.environ.get("AzureWebJobsStorage__accountName"):
             ud_key = blob_service_client.get_user_delegation_key(start_time, expiry)
             sas_token = generate_blob_sas(
                 account_name=account_name,
                 container_name=container_name,
                 blob_name=blob_name,
                 user_delegation_key=ud_key,
                 permission=BlobSasPermissions(read=True),
                 expiry=expiry,
                 start=start_time
             )
        else:
             # Connection String Logic (Dev)
             conn_str = os.environ.get("AzureWebJobsStorage")
             if conn_str:
                 items = dict(item.split('=', 1) for item in conn_str.split(';') if '=' in item)
                 key = items.get('AccountKey')
                 if key:
                     sas_token = generate_blob_sas(
                         account_name=items.get('AccountName'),
                         container_name=container_name,
                         blob_name=blob_name,
                         account_key=key,
                         permission=BlobSasPermissions(read=True),
                         expiry=expiry,
                         start=start_time
                     )

        if sas_token:
            full_url = f"{blob_url_base}?{sas_token}"
            
            # Determine Description
            desc = "Individual judge papers (ZIP)" if filename.lower().endswith('.zip') else "All judge papers (PDF)"
            
            # Form Entity
            entity = {
                "PartitionKey": competition,
                "RowKey": filename.replace('/', '_').replace('\\', '_'),
                "Url": full_url,
                "ExpirationDate": expiry.isoformat(),
                "Description": desc,
                "FileName": filename,
                "FileSize": int(file_size)
            }
            
            table_client.upsert_entity(entity)
            logging.info(f"Stored SAS link for {filename}")
            
    except Exception as e:
        logging.error(f"Error creating SAS: {e}")


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS)
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Listing competitions...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        competitions = []
        use_blob_storage_fallback = True
        table_client = None

        # Try reading from Table Storage first
        try:
            table_client = get_table_client("competitions")
            if table_client:
                # Ensure table exists
                try: table_client.create_table()
                except: pass

                entities = table_client.query_entities("PartitionKey eq 'GLOBAL'")
                for entity in entities:
                    competitions.append({
                        "name": entity.get("Name", entity["RowKey"]),
                        "createdBy": entity.get("CreatedBy", "-"),
                        "createdDate": entity.get("CreatedDate", "-")
                    })
                
                # If we found competitions in the table, we don't need to scan blobs
                # unless the table is empty, in which case we might be migrating
                if competitions:
                    use_blob_storage_fallback = False
        except Exception as e:
            logging.warning(f"Failed to query competitions table: {e}")

        if use_blob_storage_fallback:
            logging.info("Falling back to Blob Storage scan for competitions...")
            blob_service_client = get_blob_service_client()
            if not blob_service_client:
                 return func.HttpResponse("Storage configuration invalid", status_code=500)

            container_name = "fs-judgepapers"
            container_client = blob_service_client.get_container_client(container_name)
            
            if not container_client.exists():
                 return func.HttpResponse("[]", mimetype="application/json")

            # Walk blobs with delimiter to find "folders"
            for item in container_client.walk_blobs(delimiter='/'):
                if item.name.endswith('/'):
                    name = item.name.rstrip('/')
                    comp_data = {
                        "name": name,
                        "createdBy": "-",
                        "createdDate": "-"
                    }
                    
                    # Check for metadata.json
                    try:
                        metadata_blob = container_client.get_blob_client(f"{name}/metadata.json")
                        if metadata_blob.exists():
                             stream = metadata_blob.download_blob().readall()
                             meta = json.loads(stream)
                             comp_data["createdBy"] = meta.get("createdBy", "-")
                             comp_data["createdDate"] = meta.get("createdDate", "-")
                    except Exception:
                        pass
                    
                    competitions.append(comp_data)
                    
                    # Backfill table
                    if table_client:
                        try:
                            entity = {
                                "PartitionKey": "GLOBAL", 
                                "RowKey": name,
                                "Name": name,
                                "CreatedBy": comp_data["createdBy"],
                                "CreatedDate": comp_data["createdDate"]
                            }
                            table_client.upsert_entity(entity)
                        except Exception as e:
                            logging.error(f"Failed to backfill competition {name}: {e}")
                
        return func.HttpResponse(json.dumps(competitions), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error listing competitions: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


@app.route(route="create_competition", auth_level=func.AuthLevel.ANONYMOUS)
def create_competition(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Creating competition...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    name = req.params.get('name')
    if not name:
        return func.HttpResponse("Missing name parameter", status_code=400)
    
    # Sanitize name
    safe_name = "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()
    if not safe_name:
         return func.HttpResponse("Invalid name", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
             return func.HttpResponse("Storage configuration invalid", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")
        
        # Create metadata.json file to establish the "folder"
        blob_path = f"{safe_name}/metadata.json"
        
        metadata = {
            "createdBy": email,
            "createdDate": f"{datetime.utcnow().isoformat()}Z"
        }
        
        container.upload_blob(blob_path, json.dumps(metadata, indent=4), overwrite=True)
        
        # Add to competitions table for faster listing
        try:
            comp_table = get_table_client("competitions")
            if comp_table:
                # Ensure table exists
                try: comp_table.create_table()
                except: pass
                
                entity = {
                    "PartitionKey": "GLOBAL", 
                    "RowKey": safe_name,
                    "Name": safe_name,
                    "CreatedBy": metadata["createdBy"],
                    "CreatedDate": metadata["createdDate"]
                }
                comp_table.upsert_entity(entity)
        except Exception as e:
            logging.error(f"Failed to update competitions table: {e}")

        return func.HttpResponse(json.dumps({"name": safe_name}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error creating competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS)
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Deleting competition...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    name = req.params.get('name')
    if not name:
        return func.HttpResponse("Missing name parameter", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)
        
        container = blob_service_client.get_container_client("fs-judgepapers")
        
        # List all blobs with this prefix and delete them
        blobs = container.list_blobs(name_starts_with=f"{name}/")
        count = 0
        for blob in blobs:
            container.delete_blob(blob.name)
            count += 1
            
        # Delete table entities for this competition
        try:
            table_client = get_table_client()
            if table_client:
                # Query all entities for this partition key
                safe_pk = name.replace("'", "''")
                entities = table_client.query_entities(f"PartitionKey eq '{safe_pk}'")
                table_count = 0
                for entity in entities:
                    table_client.delete_entity(partition_key=entity['PartitionKey'], row_key=entity['RowKey'])
                    table_count += 1
                logging.info(f"Deleted {table_count} table rows for competition {name}")
        except Exception as table_err:
            logging.warning(f"Error deleting table entities: {table_err}")

        # Delete from competitions table
        try:
            comp_table = get_table_client("competitions")
            if comp_table:
                comp_table.delete_entity(partition_key="GLOBAL", row_key=name)
        except Exception as e:
             logging.warning(f"Error deleting from competitions table (or not found): {e}")

        return func.HttpResponse(json.dumps({"deleted": count}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _get_categories():
    """Load categories from the Azure Table, with caching."""
    try:
        table_client = get_table_client("categories")
        if table_client:
            return load_categories(table_client)
    except Exception as e:
        logging.warning(f"Failed to load categories table: {e}")
    return []


def parse_competition_file(filename: str, categories=None):
    """
    Parses the filename to extract Type, Category, Segment, JudgingMethod, and Suffix.
    Uses the 'categories' Azure Table for abbreviation matching (longest-prefix-match).
    Segment detection (QUAL/FNL) and split/group numbers are parsed generically.
    """
    try:
        if categories is None:
            categories = _get_categories()

        result = parse_filename_generic(filename, categories)
        return result
    except Exception:
        return None

@app.route(route="get_categories", auth_level=func.AuthLevel.ANONYMOUS)
def get_categories(req: func.HttpRequest) -> func.HttpResponse:
    """Returns all competition categories from the categories table."""
    logging.info('Getting categories...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        categories = _get_categories()
        return func.HttpResponse(json.dumps(categories), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting categories: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS)
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Getting competition details...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    name = req.params.get('name')
    if not name:
        return func.HttpResponse("Missing name parameter", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)
        
        container = blob_service_client.get_container_client("fs-judgepapers")
        
        # Pre-load categories once for all files
        categories = _get_categories()

        # Load competition settings from metadata.json
        competition_language = 'fi'  # Default to Finnish
        try:
            metadata_blob = container.get_blob_client(f"{name}/metadata.json")
            if metadata_blob.exists():
                meta_stream = metadata_blob.download_blob().readall()
                meta = json.loads(meta_stream)
                competition_language = meta.get('language', 'fi')
        except Exception:
            pass
        
        blobs = container.list_blobs(name_starts_with=f"{name}/")
        
        files_data = []
        structure = {} # { category: { segment: [files] } }
        
        detected_types = set()
        detected_names = set()
        detected_dates = set()
        detected_category_codes = set()
        
        # For enriching segment names:
        # Maps (categoryCode, raw_segment_marker) -> actual segment name
        prefix_segment_names = {}  # prefix -> segment name from JudgesSheetAll line 2
        schedule_blob_name = None  # Track CompetitionSchedule blob for later parsing

        for blob in blobs:
            # Filter out files in subfolders (only process root of competition folder)
            # blob.name is "{name}/{filename}"
            # We want to skip "{name}/{subfolder}/{filename}"
            if '/' in blob.name[len(name)+1:]:
                continue

            # Skip the init.md or metadata.json file
            if blob.name.endswith('init.md') or blob.name.endswith('metadata.json'):
                continue
                
            parsed = parse_competition_file(blob.name, categories)
            if parsed:
                files_data.append(parsed)
                cat = parsed['category']
                seg = parsed['segment']
                
                if parsed.get('categoryCode'):
                    detected_category_codes.add(parsed['categoryCode'])
                
                # Track CompetitionSchedule blob for later parsing
                if parsed['suffix'] == 'CompetitionSchedule.pdf':
                    schedule_blob_name = blob.name
                
                # Analyze Type (now from categories table, skip 'Competition' pseudo-type)
                if parsed.get('type') and parsed['type'] != 'Competition':
                    detected_types.add(parsed['type'])
                
                # Analyze Name (from JudgesSheetAll)
                if "JudgesDetailsAll" in parsed['suffix'] or "JudgesSheetAll" in parsed['suffix']:
                    try:
                        blob_client = container.get_blob_client(blob.name)
                        stream = io.BytesIO()
                        blob_client.download_blob().readinto(stream)
                        stream.seek(0)
                        
                        reader = PdfReader(stream)
                        if len(reader.pages) > 0:
                            # Use layout mode to respect visual order (Top-down)
                            try:
                                text = reader.pages[0].extract_text(extraction_mode="layout")
                            except Exception:
                                text = reader.pages[0].extract_text()
                                
                            if text:
                                lines = text.splitlines()
                                non_empty = [l.strip() for l in lines if l.strip()]
                                
                                # Line 1 = competition name
                                if non_empty:
                                    detected_names.add(non_empty[0])
                                    parsed['competition_name'] = non_empty[0]
                                
                                # Line 2 = full segment description (category + segment name)
                                # Extract just the segment part by stripping the category display name
                                if len(non_empty) >= 2:
                                    full_seg_line = non_empty[1]
                                    seg_name = full_seg_line
                                    
                                    # Get category display name for this file
                                    cat_display = parsed.get('category', '')
                                    # Strip "#N" split suffix if present
                                    cat_base = re.sub(r'\s*#\d+$', '', cat_display)
                                    
                                    if cat_base:
                                        upper_line = full_seg_line.upper()
                                        upper_cat = cat_base.upper()
                                        if upper_line.startswith(upper_cat):
                                            seg_name = full_seg_line[len(cat_base):].strip()
                                        else:
                                            # Fuzzy: normalize punctuation and try again
                                            norm_line = re.sub(r'[,./\-]', ' ', upper_line)
                                            norm_line = re.sub(r'\s+', ' ', norm_line).strip()
                                            norm_cat = re.sub(r'[,./\-]', ' ', upper_cat)
                                            norm_cat = re.sub(r'\s+', ' ', norm_cat).strip()
                                            if norm_line.startswith(norm_cat):
                                                # Walk original string to find split position
                                                ci = 0
                                                for i, ch in enumerate(full_seg_line):
                                                    if ci >= len(norm_cat):
                                                        seg_name = full_seg_line[i:].strip()
                                                        break
                                                    uch = ch.upper()
                                                    if uch in ',.-/' or (uch == ' ' and ci > 0 and norm_cat[ci-1] == ' '):
                                                        continue
                                                    if ci < len(norm_cat) and uch == norm_cat[ci]:
                                                        ci += 1
                                    
                                    if seg_name:
                                        file_prefix = parsed.get('prefix', '')
                                        if file_prefix:
                                            prefix_segment_names[file_prefix] = seg_name
                                            parsed['segment_display_name'] = seg_name
                    except Exception as ex:
                        logging.warning(f"Error reading PDF {blob.name}: {ex}")

                # Analyze Dates (from StartListwithTimes)
                if "StartListwithTimes" in parsed['suffix']:
                    try:
                        blob_client = container.get_blob_client(blob.name)
                        stream = io.BytesIO()
                        blob_client.download_blob().readinto(stream)
                        stream.seek(0)
                        
                        reader = PdfReader(stream)
                        if len(reader.pages) > 0:
                            try:
                                text = reader.pages[0].extract_text(extraction_mode="layout")
                            except:
                                text = reader.pages[0].extract_text()

                            if text:
                                # Look for Event Date pattern: dd MONTH yyyy (e.g. 25 OCTOBER 2025)
                                # Usually appears in header like: SATURDAY, 25 OCTOBER 2025
                                # We ignore the d.m.yyyy format to avoid capturing "Printed at" footer timestamps
                                months = r"(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
                                pattern = r"\b(\d{1,2})\s+(" + months + r")\s+(\d{4})\b"
                                
                                matches = re.findall(pattern, text, re.IGNORECASE)
                                for match in matches:
                                    day, month_name, year = match
                                    try:
                                        # Parse date (e.g. "25 October 2025")
                                        dt_str = f"{day} {month_name.title()} {year}"
                                        dt = datetime.strptime(dt_str, "%d %B %Y")
                                        
                                        # Store as YYYY-MM-DD for sorting
                                        detected_dates.add(dt.strftime("%Y-%m-%d"))
                                    except Exception as e:
                                        logging.warning(f"Date parse error: {e}")
                    except Exception as ex:
                        logging.warning(f"Error reading PDF {blob.name} for dates: {ex}")

                # Competition-wide files (e.g. CompetitionSchedule) go to a
                # separate list, not into the per-category structure.
                if parsed.get('type') == 'Competition':
                    pass  # handled below via competitionFiles
                else:
                    if cat not in structure:
                        structure[cat] = {}
                    if seg not in structure[cat]:
                        structure[cat][seg] = []
                    
                    structure[cat][seg].append(parsed)
            else:
                # Unparsed file
                if "Uncategorized" not in structure:
                    structure["Uncategorized"] = {}
                if "Files" not in structure["Uncategorized"]:
                    structure["Uncategorized"]["Files"] = []
                structure["Uncategorized"]["Files"].append({
                    "filename": blob.name.split('/')[-1],
                    "suffix": blob.name.split('/')[-1]
                })

        # ---------------------------------------------------------
        # Enrich segment names using JudgesSheetAll line 2
        # ---------------------------------------------------------
        # The raw segment keys are "QUAL", "FNL", "Category General", etc.
        # Enrich them with actual names from JudgesSheetAll (e.g., "PDK1 (Starlight Waltz)")
        # by looking up the prefix→segment_name mapping we built above.
        enriched_structure = {}
        for cat, segments in structure.items():
            enriched_structure[cat] = {}
            for seg_key, files in segments.items():
                # Find the best display name for this segment
                display_seg = seg_key
                if seg_key not in ("Category General", "General"):
                    # Look through files in this segment to find a prefix with a known name
                    for f in files:
                        file_prefix = f.get('prefix', '')
                        if file_prefix and file_prefix in prefix_segment_names:
                            display_seg = prefix_segment_names[file_prefix]
                            break
                    # If still a raw marker, apply fallback display names
                    if display_seg == seg_key:
                        if seg_key == "QUAL":
                            display_seg = "Short Program (QUAL)"
                        elif seg_key == "FNL":
                            display_seg = "Free Skating (FNL)"
                
                # Update the segment field in each file's parsed data too
                for f in files:
                    f['segment'] = display_seg
                
                if display_seg not in enriched_structure[cat]:
                    enriched_structure[cat][display_seg] = []
                enriched_structure[cat][display_seg].extend(files)
        
        structure = enriched_structure

        # Parse CompetitionSchedule if present (for schedule info in response)
        schedule_data = []
        if schedule_blob_name:
            try:
                blob_client = container.get_blob_client(schedule_blob_name)
                stream = io.BytesIO()
                blob_client.download_blob().readinto(stream)
                stream.seek(0)
                schedule_data = parse_competition_schedule(stream, categories)
            except Exception as ex:
                logging.warning(f"Error parsing CompetitionSchedule: {ex}")

        # Process metadata
        comp_type_display = list(detected_types)[0] if len(detected_types) == 1 else "Unknown" if len(detected_types) == 0 else "Mixed"
        comp_full_name = list(detected_names)[0] if len(detected_names) == 1 else "-"         
        
        # Process dates
        comp_date_display = "-"
        if detected_dates:
            sorted_dates = sorted(list(detected_dates))
            start_date = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
            end_date = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
            
            # Format: d.M.yyyy
            start_str = f"{start_date.day}.{start_date.month}.{start_date.year}"
            end_str = f"{end_date.day}.{end_date.month}.{end_date.year}"
            
            if start_str == end_str:
                comp_date_display = start_str
            else:
                comp_date_display = f"{start_str} - {end_str}"

        alerts = []
        if len(detected_names) > 1:
            alerts.append(f"Multiple competition names found: {', '.join(detected_names)}")

        # Fetch generated links
        generated_links = []
        try:
            table_client = get_table_client()
            if table_client:
                 pk = name.strip("/")
                 safe_pk = pk.replace("'", "''")
                 try:
                     entities = table_client.query_entities(f"PartitionKey eq '{safe_pk}'")
                     for entity in entities:
                         generated_links.append({
                             "fileName": entity.get("FileName"),
                             "url": entity.get("Url"),
                             "description": entity.get("Description"),
                             "expiration": entity.get("ExpirationDate"),
                             "size": entity.get("FileSize")
                         })
                 except ResourceNotFoundError:
                     pass 
        except Exception as e:
            logging.warning(f"Could not fetch generated links: {e}")

        # Collect competition-wide files (e.g. CompetitionSchedule)
        competition_files = [f for f in files_data if f.get('type') == 'Competition']

        return func.HttpResponse(json.dumps({
            "name": name,
            "fullName": comp_full_name,
            "type": comp_type_display,
            "date": comp_date_display,
            "language": competition_language,
            "files": files_data,
            "structure": structure,
            "competitionFiles": competition_files,
            "alerts": alerts,
            "categories": list(detected_category_codes),
            "generatedFiles": generated_links
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting details: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="save_competition_settings", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def save_competition_settings(req: func.HttpRequest) -> func.HttpResponse:
    """Save competition settings (e.g. language) to metadata.json."""
    logging.info('Saving competition settings...')

    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        req_body = req.get_json()
        competition_name = req_body.get('name')
        settings = req_body.get('settings', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    if not competition_name:
        return func.HttpResponse("Missing name parameter", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")
        metadata_blob = container.get_blob_client(f"{competition_name}/metadata.json")

        # Read existing metadata
        existing_meta = {}
        try:
            if metadata_blob.exists():
                stream = metadata_blob.download_blob().readall()
                existing_meta = json.loads(stream)
        except Exception:
            pass

        # Merge new settings into existing metadata
        for key, value in settings.items():
            existing_meta[key] = value

        # Write back
        container.upload_blob(
            f"{competition_name}/metadata.json",
            json.dumps(existing_meta, indent=4),
            overwrite=True
        )

        return func.HttpResponse("Settings saved", status_code=200)
    except Exception as e:
        logging.error(f"Error saving settings: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="generate_judging_papers", auth_level=func.AuthLevel.ANONYMOUS)
def generate_judging_papers(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # 1. Security Check
    email = get_user_email_from_header(req)
    
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(email):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)

    try:
        req_body = req.get_json()
        working_folder = req_body.get('workingFolder')
        options = req_body.get('options', {}) 
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    if not working_folder:
        return func.HttpResponse("Please pass a workingFolder in the request body", status_code=400)

    # Connect to Blob Storage
    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
             return func.HttpResponse("Storage configuration not found (AzureWebJobsStorage or AzureWebJobsStorage__accountName)", status_code=500)
            
        logging.info("Connected to Blob Storage")
    
        container_name = "fs-judgepapers"
        container_client = blob_service_client.get_container_client(container_name)

        # Create temp directories
        temp_dir = tempfile.mkdtemp()
        source_dir = os.path.join(temp_dir, "source")
        output_dir = os.path.join(temp_dir, "output")
        os.makedirs(source_dir)
        os.makedirs(output_dir)

        # Download files
        logging.info(f"Downloading files from {working_folder}...")
        blobs = container_client.list_blobs(name_starts_with=working_folder)
        download_count = 0
        for blob in blobs:
            # Calculate relative path to maintain structure inside the working folder
            clean_working_folder = working_folder.strip("/")
            if blob.name.startswith(clean_working_folder + "/"):
                relative_path = blob.name[len(clean_working_folder)+1:]
            elif blob.name == clean_working_folder:
                continue 
            else:
                if not blob.name.startswith(clean_working_folder + "/"):
                    continue
                relative_path = blob.name[len(clean_working_folder)+1:]

            local_path = os.path.join(source_dir, relative_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            with open(local_path, "wb") as download_file:
                download_file.write(container_client.download_blob(blob.name).readall())
            download_count += 1
        
        logging.info(f"Downloaded {download_count} files.")

        if download_count == 0:
             return func.HttpResponse(f"No files found in folder '{working_folder}'", status_code=404)

        # Run the processor
        logging.info("Running processor...")
        process_judging_papers(source_dir, output_dir, options=options)

        # Upload results
        logging.info("Uploading results...")
        upload_count = 0
        for root, dirs, files in os.walk(output_dir):
            for file in files:
                local_file_path = os.path.join(root, file)
                relative_path = os.path.relpath(local_file_path, output_dir)
                
                # Upload to workingFolder/judgePapers/relative_path
                blob_name = f"{clean_working_folder}/judgePapers/{relative_path}".replace("\\", "/")
                
                # Get file size
                file_size = os.path.getsize(local_file_path)

                with open(local_file_path, "rb") as data:
                    container_client.upload_blob(name=blob_name, data=data, overwrite=True)
                upload_count += 1
                
                # Check for generate files (PDF summaries or ZIPs)
                if file.lower().endswith('.zip') or (file.lower().startswith('judgingpapers_') and file.lower().endswith('.pdf')):
                     create_and_store_sas_link(blob_service_client, "fs-judgepapers", blob_name, clean_working_folder, file, file_size)
                
        logging.info(f"Uploaded {upload_count} files.")
        
        return func.HttpResponse(f"Successfully processed {download_count} files and generated {upload_count} output files.", status_code=200)

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        return func.HttpResponse("Error processing request. Check server logs for details.", status_code=500)
    finally:
        # Cleanup
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
@app.route(route="upload_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def upload_file(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Uploading file...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    competition = req.params.get('competition')
    filename = req.params.get('filename')
    
    if not competition or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)
    
    # Sanitize filename: strip path traversal characters
    filename = os.path.basename(filename)
    
    if not filename.lower().endswith('.pdf'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)

    # Check file size limit
    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

    try:
        file_content = req.get_body()
        
        if len(file_content) > MAX_UPLOAD_SIZE:
            return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
        
        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)
        
        container = blob_service_client.get_container_client("fs-judgepapers")
        
        blob_path = f"{competition}/{filename}"
        
        container.upload_blob(blob_path, file_content, overwrite=True)
        
        return func.HttpResponse(json.dumps({"filename": filename, "status": "uploaded"}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error uploading file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

@app.route(route="delete_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["DELETE", "POST"])
def delete_file(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Deleting file...')
    
    email = get_user_email_from_header(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    competition = req.params.get('competition')
    filename = req.params.get('filename')
    
    if not competition or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)

    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)
        
        container = blob_service_client.get_container_client("fs-judgepapers")
        
        blob_path = f"{competition}/{filename}"
        
        if container.get_blob_client(blob_path).exists():
            container.delete_blob(blob_path)
            
            # Try to delete associated table entity (if distinct generated file)
            try:
                table_client = get_table_client()
                if table_client:
                    simple_filename = os.path.basename(filename)
                    row_key = simple_filename.replace('/', '_').replace('\\', '_')
                    # We ignore errors if the entity does not exist
                    table_client.delete_entity(partition_key=competition, row_key=row_key)
                    logging.info(f"Deleted table entity for {simple_filename}")
            except ResourceNotFoundError:
                pass
            except Exception as table_err:
                logging.warning(f"Error deleting table entity: {table_err}")

            return func.HttpResponse(json.dumps({"status": "deleted"}), mimetype="application/json")
        else:
            return func.HttpResponse("File not found", status_code=404)
            
    except Exception as e:
        logging.error(f"Error deleting file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)
