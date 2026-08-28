"""
Simple Logger for User-specific Logging
File: api/utils/logger.py
"""
import logging
from pathlib import Path
from datetime import datetime

# Create logs directory
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)


def get_user_logger(client_ip: str):
    """
    Get a logger that writes to IP-specific log file
   
    Args:
        client_ip: Client's IP address
       
    Returns:
        Logger instance
    """
    # Clean IP for filename (replace dots and colons)
    clean_ip = client_ip.replace('.', '_').replace(':', '_')
   
    # Create IP-specific directory
    ip_dir = LOGS_DIR / clean_ip
    ip_dir.mkdir(exist_ok=True)
   
    # Create log file with today's date
    date_str = datetime.now().strftime('%Y-%m-%d')
    log_file = ip_dir / f"{date_str}.log"
   
    # Setup logger
    logger = logging.getLogger(f"user_{clean_ip}")
    logger.setLevel(logging.INFO)
   
    # Clear existing handlers to avoid duplicates
    logger.handlers.clear()
   
    # File handler
    file_handler = logging.FileHandler(log_file, mode='a')
    file_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
   
    # Console handler
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)
   
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
   
    return logger
