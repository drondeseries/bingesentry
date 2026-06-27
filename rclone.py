import os
import psutil
import shlex
import subprocess
import logging

def is_file_being_cached(file_path):
    """
    Checks if there is already an active process caching this file.
    Matches if any part of the process's command line contains the target file path.
    """
    try:
        file_path_abs = os.path.abspath(file_path)
        current_pid = os.getpid()
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            if proc.pid == current_pid:
                continue
            
            cmdline = proc.info.get('cmdline')
            if not cmdline:
                continue
                
            for arg in cmdline:
                try:
                    arg_abs = os.path.abspath(arg)
                except Exception:
                    arg_abs = ""
                    
                if file_path == arg or file_path_abs == arg or (arg_abs and file_path_abs == arg_abs):
                    logging.info(f"Found active caching process: PID {proc.pid} ({proc.info['name']}) is handling '{file_path}'")
                    return True
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        logging.debug(f"Error checking processes: {e}")
        return False

def start_cache_process(command_template, file_path):
    """
    Spawns a decoupled background process to cache the target file path.
    Replaces {file_path} in the command template.
    """
    try:
        cmd_str = command_template.format(file_path=file_path)
        cmd_args = shlex.split(cmd_str)
        
        logging.info(f"Launching cache process: {cmd_str}")
        # start_new_session=True decouples the process group so it lives on after the python script finishes
        process = subprocess.Popen(
            cmd_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return process
    except Exception as e:
        logging.error(f"Error starting cache process for '{file_path}': {e}")
        return None
