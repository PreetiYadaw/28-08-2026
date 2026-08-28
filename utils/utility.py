import fitz  # PyMuPDF
from docx import Document
from io import BytesIO

def extract_text_from_file(file_content: bytes, filename: str) -> str:
    """
    Extract text from PDF or DOCX file content.
   
    Args:
        file_content: Binary content of the file
        filename: Name of the file to determine type
   
    Returns:
        Extracted text as string
    """
    try:
        if filename.lower().endswith('.pdf'):
            # Extract from PDF using PyMuPDF
            pdf_document = fitz.open(stream=file_content, filetype="pdf")
            text = ""
            for page_num in range(pdf_document.page_count):
                page = pdf_document[page_num]
                text += page.get_text() + "\n"
            pdf_document.close()
            return text.strip()
       
        elif filename.lower().endswith('.docx'):
            # Extract from DOCX
            doc = Document(BytesIO(file_content))
            text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
            return text.strip()
       
        else:
            raise ValueError(f"Unsupported file type: {filename}")
   
    except Exception as e:
        raise ValueError(f"Failed to extract text from {filename}: {str(e)}")
