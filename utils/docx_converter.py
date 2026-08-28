import os
from pathlib import Path

# Try to import the v2 converter as fallback
try:
    from api.utils.docx_converter_v2 import docx_to_pdf as docx_to_pdf_v2
    V2_AVAILABLE = True
except ImportError:
    V2_AVAILABLE = False
    print("Warning: docx_converter_v2 not available")


def convert_docx_to_pdf(docx_path: str, output_pdf_path: str = None) -> str:
    """
    Convert a DOCX file to PDF with automatic fallback to v2 converter.
   
    Args:
        docx_path: Path to the DOCX file
        output_pdf_path: Optional path for output PDF. If None, saves in same directory
       
    Returns:
        str: Path to the converted PDF file
       
    Raises:
        FileNotFoundError: If DOCX file doesn't exist
        Exception: If conversion fails
    """
    # Check if file exists
    if not os.path.exists(docx_path):
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")
   
    # Verify it's a DOCX file
    if not docx_path.lower().endswith('.docx'):
        raise ValueError(f"File is not a DOCX file: {docx_path}")
   
    # Generate output path if not provided
    if output_pdf_path is None:
        docx_file = Path(docx_path)
        output_pdf_path = str(docx_file.parent / f"{docx_file.stem}.pdf")
   
    print(f"Converting DOCX to PDF...")
    print(f"  Input: {docx_path}")
    print(f"  Output: {output_pdf_path}")
   
    # Try primary conversion method (docx2pdf)
    try:
        from docx2pdf import convert
        convert(docx_path, output_pdf_path)
   
        # Verify the PDF was created
        if os.path.exists(output_pdf_path):
            print(f"✓ PDF created successfully using primary method: {output_pdf_path}")
            return output_pdf_path
        else:
            raise Exception("PDF file was not created by primary method")
       
    except Exception as e:
        print(f"⚠ Primary conversion failed: {str(e)}")
   
        # Try fallback converter (v2)
        if V2_AVAILABLE:
            print("🔄 Attempting conversion with fallback method (v2)...")
            try:
                success = docx_to_pdf_v2(docx_path, output_pdf_path)
               
                if success and os.path.exists(output_pdf_path):
                    print(f"✓ PDF created successfully using fallback method: {output_pdf_path}")
                    return output_pdf_path
                else:
                    raise Exception("Fallback conversion did not produce a valid PDF")
                   
            except Exception as fallback_error:
                print(f"✗ Fallback conversion also failed: {str(fallback_error)}")
                raise Exception(
                    f"Both conversion methods failed.\n"
                    f"Primary error: {str(e)}\n"
                    f"Fallback error: {str(fallback_error)}"
                )
        else:
            # No fallback available
            raise Exception(
                f"Primary conversion failed and fallback (v2) is not available.\n"
                f"Error: {str(e)}"
            )

def get_file_type(file_path: str) -> str:
    """
    Determine if file is PDF or DOCX.
   
    Args:
        file_path: Path to the file
       
    Returns:
        str: 'pdf' or 'docx'
       
    Raises:
        ValueError: If file type is not supported
    """
    file_lower = file_path.lower()
   
    if file_lower.endswith('.pdf'):
        return 'pdf'
    elif file_lower.endswith('.docx'):
        return 'docx'
    else:
        raise ValueError(f"Unsupported file type. Must be PDF or DOCX: {file_path}")


def prepare_file_for_processing(file_path: str, temp_dir: str = None) -> tuple[str, bool]:
    """
    Prepare file for processing. Converts DOCX to PDF if needed.
   
    Args:
        file_path: Path to the input file (PDF or DOCX)
        temp_dir: Optional directory for temporary PDF files
       
    Returns:
        tuple: (pdf_path, is_converted)
            - pdf_path: Path to PDF file (original or converted)
            - is_converted: True if file was converted from DOCX
           
    Raises:
        Exception: If file preparation fails
    """
    try:
        file_type = get_file_type(file_path)
       
        if file_type == 'pdf':
            print(f"✓ File is already PDF: {file_path}")
            return file_path, False
       
        elif file_type == 'docx':
            print(f"📄 File is DOCX, converting to PDF...")
           
            # Generate temporary PDF path
            if temp_dir:
                os.makedirs(temp_dir, exist_ok=True)
                file_name = Path(file_path).stem
                temp_pdf_path = os.path.join(temp_dir, f"{file_name}_converted.pdf")
            else:
                # Save in same directory as DOCX
                temp_pdf_path = None
           
            # Convert DOCX to PDF
            pdf_path = convert_docx_to_pdf(file_path, temp_pdf_path)
            return pdf_path, True
       
    except Exception as e:
        raise Exception(f"File preparation failed: {str(e)}")
