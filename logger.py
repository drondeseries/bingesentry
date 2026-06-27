import logging
import os
import re
from logging.handlers import RotatingFileHandler

# Regular expression to strip ANSI color escape codes from log files
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

class StripAnsiFormatter(logging.Formatter):
    """
    Log formatter that strips ANSI escape sequences (colors) from the logged messages.
    Perfect for keeping log files clean while maintaining colors in the terminal.
    """
    def format(self, record):
        # We must format the record first using the parent formatter
        formatted = super().format(record)
        return ANSI_ESCAPE.sub('', formatted)

def setup_logger(log_file, log_level_str="INFO", log_to_console=True):
    """
    Sets up a root logger that can simultaneously output to stdout (with colors)
    and to a rotating log file (without colors).
    """
    level = getattr(logging, log_level_str.upper(), logging.INFO)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear any existing handlers to prevent duplicate logging
    root_logger.handlers = []
    
    # 1. Setup File Handler (strips ANSI color codes)
    if log_file:
        try:
            log_dir = os.path.dirname(os.path.abspath(log_file))
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
                
            file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
            file_formatter = StripAnsiFormatter('%(asctime)s %(levelname)s: %(message)s')
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            print(f"Failed to setup file logging to {log_file}: {e}")
            
    # 2. Setup Console Handler (retains ANSI color codes)
    if log_to_console:
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)
        
    return root_logger
