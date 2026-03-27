import os
import json
import tempfile
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from supabase import create_client, Client
import google.generativeai as genai
from dotenv import load_dotenv

# --- 1. INITIALIZATION ---
load_dotenv()

app = FastAPI(title="LHDN Vault API", description="API for extracting and storing tax receipts.")

# Allow frontend to communicate with this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # We can restrict this to your domain later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup Supabase
supabase: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# Setup Gemini
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-2.5-flash', 
                              generation_config={"response_mime_type": "application/json"})

EXTRACTION_PROMPT = """
Analyze this receipt and extract the following information into a strict JSON format. 
If a value is not found, use null.
The JSON keys must be exactly:
- receipt_date (format: YYYY-MM-DD)
- merchant_name (string)
- total_amount (number, do not include currency symbols)
- tax_category (string, MUST be exactly one of the following: 
    'Lifestyle - Books/Internet/Sports/Electronics', 
    'Medical Expenses',
    'Dentistry', 
    'Childcare Equipment', 
    'Nursery',
    'Life Insurance', 
    'Education', 
    'Donations', 
    'Zakat',
    'Other/Uncategorized')
- purchased_items (string, a brief comma-separated list of the main items or services purchased. Keep it concise.)
"""

# --- 2. THE PIPELINE ENDPOINT ---
@app.post("/api/upload-receipt/")
async def upload_receipt(file: UploadFile = File(...)):
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/jpg", "application/pdf"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type. Only JPG, PNG, and PDF are allowed.")

    file_extension = file.filename.split('.')[-1]
    
    # Read the file data into memory
    file_bytes = await file.read()

    # Create a temporary file for Gemini and Supabase to process
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_extension}") as tmp_file:
        tmp_file.write(file_bytes)
        tmp_file_path = tmp_file.name

    try:
        # --- EXTRACT & TRANSFORM (Gemini) ---
        gemini_file = genai.upload_file(path=tmp_file_path)
        response = model.generate_content([EXTRACTION_PROMPT, gemini_file])
        extracted_data = json.loads(response.text)
        
        # --- LOAD (Supabase) ---
        # 1. Upload physical file to Storage
        safe_filename = f"{os.urandom(8).hex()}_{file.filename}"
        with open(tmp_file_path, "rb") as f:
            supabase.storage.from_("receipts").upload(
                path=safe_filename, 
                file=f, 
                file_options={"content-type": file.content_type}
            )
        
        # 2. Insert structured metadata to DB
        db_record = {
            "receipt_date": extracted_data.get("receipt_date"),
            "merchant_name": extracted_data.get("merchant_name"),
            "total_amount": extracted_data.get("total_amount"),
            "tax_category": extracted_data.get("tax_category", "Other/Uncategorized"),
            "purchased_items": extracted_data.get("purchased_items", "Details not found"),
            "owner_name": owner_name,
            "file_path": safe_filename
        }
        
        db_response = supabase.table("tax_receipts").insert(db_record).execute()
        
        # Cleanup Gemini file
        genai.delete_file(gemini_file.name)

        return {
            "status": "success",
            "message": "Receipt processed and stored safely.",
            "data": db_response.data[0]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")
        
    finally:
        os.remove(tmp_file_path)

# --- 3. FETCH RECENT RECEIPTS ---
@app.get("/api/receipts/")
def get_recent_receipts(limit: int = 1000):
    try:
        response = supabase.table("tax_receipts").select("*").order("created_at", desc=True).limit(limit).execute()
        return {"status": "success", "data": response.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")