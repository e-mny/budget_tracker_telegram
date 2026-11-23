"""Local OCR only. All extracted fields remain suggestions until the owner confirms."""
import io
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import core

MAX_RECEIPT = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000


def parse_receipt(text):
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    merchant=next((line for line in lines[:8] if len(re.findall(r'[A-Za-z]',line))>=3 and not re.search(r'^(receipt|invoice|tax invoice|welcome|thank you)\b',line,re.I)), '')[:100]
    currency='SGD'
    for pattern,code in [(r'\b(?:SGD|S\$)', 'SGD'),(r'\b(?:USD|US\$)','USD'),(r'\bMYR\b|\bRM\s*\d','MYR'),(r'\bEUR\b|€','EUR'),(r'\bGBP\b|£','GBP'),(r'\bJPY\b|¥','JPY'),(r'\bTHB\b|฿','THB'),(r'\bAUD\b|A\$','AUD')]:
        if re.search(pattern,text,re.I):
            currency=code
            break
    candidates=[]
    for line in lines:
        if re.search(r'\b(?:grand\s+total|total\s+(?:due|paid)|amount\s+(?:due|paid)|net\s+total|total)\b',line,re.I) and not re.search(r'\b(?:sub\s*total|subtotal|tax|gst|saving|discount)\b',line,re.I):
            values=re.findall(r'(?<![\d/])\d{1,9}(?:,\d{3})*(?:\.\d{2})\b',line)
            if values:
                priority=2 if re.search(r'grand|amount due|total due|total paid',line,re.I) else 1
                candidates.append((priority,values[-1].replace(',','')))
    amount=sorted(candidates,key=lambda x:x[0])[-1][1] if candidates else ''
    if not amount and currency=='JPY':
        for line in lines:
            if re.search(r'\btotal\b',line,re.I) and not re.search(r'subtotal|tax|gst',line,re.I):
                values=re.findall(r'\d[\d,]*',line)
                if values:
                    amount=values[-1].replace(',','')
    spent_on=core.today().isoformat()
    found=False
    for pattern,order in [(r'\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b','ymd'),(r'\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b','dmy')]:
        for match in re.finditer(pattern,text):
            a,b,c=map(int,match.groups())
            try:
                candidate=date(a,b,c) if order=='ymd' else date(c,b,a)
                if candidate<=core.today():
                    spent_on=candidate.isoformat(); found=True; break
            except ValueError:
                continue
        if found:
            break
    cat='other'
    cues=[('food',r'cafe|coffee|restaurant|chicken|rice|supermarket|grocer|bakery|food|noodle'),
          ('transport',r'taxi|grab ride|mrt|parking|petrol|transit'),('shopping',r'salon|haircut|barber|facial'),
          ('health',r'clinic|hospital|pharmacy|dental'),('shopping',r'clothing|apparel|shoes|fashion'),('travel',r'hotel|airline|flight')]
    for key,pattern in cues:
        if re.search(pattern,text,re.I):
            cat=key;break
    return {'amount':amount if currency=='SGD' else '', 'category':cat,'note':merchant,'spent_on':spent_on,
            'original_currency':currency if currency!='SGD' else None,'original_amount':amount if currency!='SGD' else None,
            'ocr_text':text[:12000], 'warning':'Review the merchant, total, category and date before saving.' + (' Enter the actual SGD amount charged; no exchange rate is assumed.' if currency!='SGD' else '')}


def extract_receipt(content):
    if not content or len(content)>MAX_RECEIPT:
        raise ValueError('Upload a JPG, PNG or WebP receipt up to 10 MB.')
    if not shutil.which('tesseract'):
        raise ValueError('Local OCR is unavailable. Install Tesseract or rebuild the Docker image; you can still add the expense manually.')
    try:
        with Image.open(io.BytesIO(content)) as original:
            if original.format not in ('JPEG','PNG','WEBP') or original.width*original.height>MAX_PIXELS:
                raise ValueError('Use a JPG, PNG or WebP image up to 25 megapixels.')
            image=ImageOps.exif_transpose(original).convert('RGB')
            image.thumbnail((3200,4200))
            image=ImageOps.autocontrast(ImageOps.grayscale(image))
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError):
        raise ValueError('This image could not be read. Use a JPG, PNG or WebP receipt.') from None
    with tempfile.TemporaryDirectory(prefix='penny-receipt-') as directory:
        path=Path(directory)/'receipt.png'
        image.save(path)
        try:
            result=subprocess.run(['tesseract',str(path),'stdout','-l','eng','--psm','6'],capture_output=True,text=True,timeout=40,check=False)
        except subprocess.TimeoutExpired:
            raise ValueError('Receipt extraction timed out. Try a cropped, clearer photo.') from None
        if result.returncode:
            raise ValueError('Receipt extraction failed. Try a clearer image or enter the expense manually.')
    draft=parse_receipt(result.stdout)
    if not draft['ocr_text'].strip():
        raise ValueError('No text was detected. Try a brighter, sharper photo.')
    return draft
