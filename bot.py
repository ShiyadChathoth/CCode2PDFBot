from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler
)
import subprocess
import os
import logging
import asyncio
from asyncio.subprocess import PIPE
import telegram.error
import html
import time
import datetime
import re
import unicodedata

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram Bot Token
TOKEN = os.getenv('TOKEN')

if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

# States for ConversationHandler
CODE, RUNNING, TITLE_INPUT = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.'
    )
    return CODE

def clean_whitespace(code):
    """Clean non-standard whitespace characters from code."""
    # Replace non-breaking spaces (U+00A0) with regular spaces
    cleaned_code = code.replace('\u00A0', ' ')
    
    # Replace other problematic Unicode whitespace characters
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    original_code = update.message.text
    
    # Clean whitespace characters that might cause compilation issues
    code = clean_whitespace(original_code)
    
    # Check if code was modified during cleaning
    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code that would cause compilation errors."
        )
    
    context.user_data['code'] = code
    context.user_data['output'] = []
    context.user_data['inputs'] = []
    context.user_data['errors'] = []
    context.user_data['waiting_for_input'] = False
    context.user_data['execution_log'] = []  # Track full execution flow
    context.user_data['output_buffer'] = ""  # Buffer for incomplete output lines
    context.user_data['terminal_log'] = []  # Raw terminal output for exact formatting
    context.user_data['program_completed'] = False  # Flag to track if program has completed
    
    # Detect algorithm type from code
    algorithm_type = detect_algorithm_type(code)
    context.user_data['algorithm_type'] = algorithm_type
    
    try:
        with open("temp.c", "w") as file:
            file.write(code)
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
            # Add compilation success to execution log
            context.user_data['execution_log'].append({
                'type': 'system',
                'message': 'Code compiled successfully!',
                'timestamp': datetime.datetime.now()
            })
            
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "./temp",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE
            )
            
            context.user_data['process'] = process
            await update.message.reply_text("Code compiled successfully! Running now...")
            
            # Start monitoring process output without immediately asking for input
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            # Check if there are still whitespace errors after cleaning
            if "stray" in compile_result.stderr and "\\302" in compile_result.stderr:
                # Try a more aggressive cleaning approach
                code = re.sub(r'[^\x00-\x7F]+', ' ', code)  # Replace all non-ASCII chars with spaces
                
                with open("temp.c", "w") as file:
                    file.write(code)
                
                # Try compiling again
                compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
                
                if compile_result.returncode == 0:
                    context.user_data['execution_log'].append({
                        'type': 'system',
                        'message': 'Code compiled successfully after aggressive whitespace cleaning!',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    process = await asyncio.create_subprocess_exec(
                        "stdbuf", "-o0", "./temp",
                        stdin=PIPE,
                        stdout=PIPE,
                        stderr=PIPE
                    )
                    
                    context.user_data['process'] = process
                    await update.message.reply_text("Code compiled successfully after fixing whitespace issues! Running now...")
                    
                    # Start monitoring process output without immediately asking for input
                    asyncio.create_task(read_process_output(update, context))
                    return RUNNING
            
            # Add compilation error to execution log
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Compilation Error:\n{compile_result.stderr}",
                'timestamp': datetime.datetime.now()
            })
            
            # Provide helpful error message for whitespace issues
            if "stray" in compile_result.stderr and ("\\302" in compile_result.stderr or "\\240" in compile_result.stderr):
                await update.message.reply_text(
                    f"Compilation Error (non-standard whitespace characters):\n{compile_result.stderr}\n\n"
                    f"Your code contains invisible non-standard whitespace characters that the compiler cannot process. "
                    f"Try retyping the code in a plain text editor or use a code editor like VS Code."
                )
            else:
                await update.message.reply_text(f"Compilation Error:\n{compile_result.stderr}")
            
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

def detect_algorithm_type(code):
    """Detect the type of algorithm from the code."""
    code_lower = code.lower()
    
    # Check for specific algorithm keywords
    if "fcfs" in code_lower or "first come first serve" in code_lower:
        return "FCFS"
    elif "sjf" in code_lower or "shortest job first" in code_lower:
        return "SJF"
    elif "priority" in code_lower and "scheduling" in code_lower:
        return "Priority"
    elif "round robin" in code_lower or "roundrobin" in code_lower:
        return "Round Robin"
    elif "bestfit" in code_lower or "best fit" in code_lower:
        return "Best Fit"
    elif "worstfit" in code_lower or "worst fit" in code_lower:
        return "Worst Fit"
    elif "firstfit" in code_lower or "first fit" in code_lower:
        return "First Fit"
    
    # Check for function names
    if re.search(r'void\s+fcfs', code_lower) or re.search(r'int\s+fcfs', code_lower):
        return "FCFS"
    elif re.search(r'void\s+sjf', code_lower) or re.search(r'int\s+sjf', code_lower):
        return "SJF"
    elif re.search(r'void\s+priority', code_lower) or re.search(r'int\s+priority', code_lower):
        return "Priority"
    elif re.search(r'void\s+round_?robin', code_lower) or re.search(r'int\s+round_?robin', code_lower):
        return "Round Robin"
    elif re.search(r'void\s+best_?fit', code_lower) or re.search(r'int\s+best_?fit', code_lower):
        return "Best Fit"
    elif re.search(r'void\s+worst_?fit', code_lower) or re.search(r'int\s+worst_?fit', code_lower):
        return "Worst Fit"
    elif re.search(r'void\s+first_?fit', code_lower) or re.search(r'int\s+first_?fit', code_lower):
        return "First Fit"
    
    # If no specific algorithm is detected, try to infer from code patterns
    if "waiting time" in code_lower and "turnaround time" in code_lower:
        if "burst" in code_lower:
            return "FCFS"  # Default to FCFS for CPU scheduling
    elif "memory" in code_lower and "allocation" in code_lower:
        if "best" in code_lower:
            return "Best Fit"
        elif "worst" in code_lower:
            return "Worst Fit"
        elif "first" in code_lower:
            return "First Fit"
        else:
            return "Memory Management"  # Generic memory management
    
    # Default to "C Program" if no specific algorithm is detected
    return "C Program"

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    execution_log = context.user_data['execution_log']
    output_buffer = context.user_data['output_buffer']
    terminal_log = context.user_data['terminal_log']
    
    # Flag to track if we've seen any output that might indicate input is needed
    output_seen = False
    
    # Use a smaller read size to capture output more frequently
    read_size = 1024
    
    while True:
        # Read stdout and stderr in chunks rather than lines
        stdout_task = asyncio.create_task(process.stdout.read(read_size))
        stderr_task = asyncio.create_task(process.stderr.read(read_size))
        
        # Wait for either stdout or stderr to have data, or for a short timeout
        done, pending = await asyncio.wait(
            [stdout_task, stderr_task],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.5  # Add timeout to check process status periodically
        )

        # If no output received, check if process has ended
        if not done:
            for task in pending:
                task.cancel()
            
            # Check if process has finished
            if process.returncode is not None:
                # Process ended without more output
                # If there's any remaining data in the buffer, process it
                if output_buffer:
                    # Process any remaining buffered output
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                if output_seen:
                    # Only send completion message if we've seen some output
                    execution_log.append({
                        'type': 'system',
                        'message': 'Program execution completed.',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    # Mark program as completed
                    context.user_data['program_completed'] = True
                    
                    await update.message.reply_text("Program execution completed.")
                    
                    # Ask for program title before generating PDF
                    await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                    return TITLE_INPUT
                else:
                    # No output seen, just wait a bit more
                    await asyncio.sleep(0.1)
                    continue
            
            # If we've seen output but no new output for a while, it might be waiting for input
            if output_seen and not context.user_data.get('waiting_for_input', False):
                # Process any buffered output before prompting for input
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                context.user_data['waiting_for_input'] = True
                execution_log.append({
                    'type': 'system',
                    'message': 'Program appears to be waiting for input. Please provide input (or type "done" to finish):',
                    'timestamp': datetime.datetime.now()
                })
                await update.message.reply_text("Program appears to be waiting for input. Please provide input (or type 'done' to finish):")
            
            continue

        # Handle stdout (program output)
        if stdout_task in done:
            stdout_chunk = await stdout_task
            if stdout_chunk:
                decoded_chunk = stdout_chunk.decode()
                output_seen = True
                
                # Store raw output for exact terminal formatting
                terminal_log.append(decoded_chunk)
                
                # Append to buffer and process
                output_buffer += decoded_chunk
                context.user_data['output_buffer'] = output_buffer
                
                # Process the buffer
                output_buffer = process_output_chunk(context, output_buffer, update)
                context.user_data['output_buffer'] = output_buffer

        # Handle stderr (errors)
        if stderr_task in done:
            stderr_chunk = await stderr_task
            if stderr_chunk:
                decoded_chunk = stderr_chunk.decode()
                
                # Process error lines
                for line in decoded_chunk.splitlines(True):  # Keep line endings
                    errors.append(line.strip())
                    
                    # Add to execution log
                    execution_log.append({
                        'type': 'error',
                        'message': line.strip(),
                        'timestamp': datetime.datetime.now(),
                        'raw': line  # Store raw output with newlines
                    })
                    
                    await update.message.reply_text(f"Error: {line.strip()}")

        # Cancel pending tasks
        for task in pending:
            task.cancel()

        # Check if process has finished
        if process.returncode is not None:
            # Process any remaining buffered output
            if output_buffer:
                process_output_chunk(context, output_buffer, update)
            
            execution_log.append({
                'type': 'system',
                'message': 'Program execution completed.',
                'timestamp': datetime.datetime.now()
            })
            
            # Mark program as completed
            context.user_data['program_completed'] = True
            
            await update.message.reply_text("Program execution completed.")
            
            # Ask for program title before generating PDF
            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
            return TITLE_INPUT

def process_output_chunk(context, buffer, update):
    """Process the output buffer, extracting complete lines and preserving partial lines."""
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    
    # Split the buffer into lines, preserving the line endings
    lines = re.findall(r'[^\n]*\n|[^\n]+$', buffer)
    
    # If the buffer doesn't end with a newline, keep the last part for next time
    new_buffer = ""
    if lines and not buffer.endswith('\n'):
        new_buffer = lines[-1]
        lines = lines[:-1]
    
    # Process each complete line
    for line in lines:
        line_stripped = line.strip()
        if line_stripped:
            output.append(line_stripped)
            
            # Enhanced prompt detection - check for various patterns
            # 1. Standard prompt endings
            # 2. Words like "Enter", "Input", "Type" followed by any text
            # 3. Phrases asking for input without proper spacing
            is_prompt = (
                line_stripped.rstrip().endswith((':','>','?')) or
                re.search(r'(Enter|Input|Type|Provide|Give)(\s|\w)*', line_stripped, re.IGNORECASE) or
                "number" in line_stripped.lower()
            )
            
            # Add to execution log with appropriate type
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line  # Store raw output with newlines
            }
            
            execution_log.append(log_entry)
            
            # Display to user with appropriate prefix
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(update.message.reply_text(f"{prefix} {line_stripped}"))
    
    return new_buffer

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    execution_log = context.user_data['execution_log']
    terminal_log = context.user_data['terminal_log']

    # Check if program has completed and we're waiting for title input
    if context.user_data.get('program_completed', False):
        return await handle_title_input(update, context)

    if not process or process.returncode is not None:
        # If program has completed, treat this as title input
        if context.user_data.get('program_completed', False):
            return await handle_title_input(update, context)
        else:
            await update.message.reply_text("Program is not running anymore.")
            return ConversationHandler.END

    if user_input.lower() == 'done':
        execution_log.append({
            'type': 'system',
            'message': 'User terminated the program.',
            'timestamp': datetime.datetime.now()
        })
        await process.stdin.drain()
        process.stdin.close()
        await process.wait()
        
        # Mark program as completed
        context.user_data['program_completed'] = True
        
        # Ask for program title before generating PDF
        await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
        return TITLE_INPUT
    
    # Add user input to execution log
    execution_log.append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    # Add user input to terminal log with newline
    terminal_log.append(user_input + "\n")
    
    # Send input to process
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    
    # Acknowledge the input
    await update.message.reply_text(f"Input sent: {user_input}")
    
    return RUNNING

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    algorithm_type = context.user_data.get('algorithm_type', 'C Program')
    
    if title.lower() == 'skip':
        # Use algorithm type as default title
        context.user_data['program_title'] = algorithm_type
    else:
        # Use user-provided title
        context.user_data['program_title'] = title
    
    await update.message.reply_text(f"Using title: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program")
        algorithm_type = context.user_data.get('algorithm_type', 'C Program')
        
        # Sort execution log by timestamp to ensure correct order
        execution_log.sort(key=lambda x: x['timestamp'])
        
        # Filter execution log to keep only compilation success and program completion messages
        filtered_execution_log = [
            entry for entry in execution_log 
            if entry['type'] == 'system' and (
                'compiled successfully' in entry['message'] or 
                'execution completed' in entry['message']
            )
        ]
        
        # Extract program inputs, outputs, and prompts
        inputs = [entry for entry in execution_log if entry['type'] == 'input']
        outputs = [entry for entry in execution_log if entry['type'] == 'output']
        prompts = [entry for entry in execution_log if entry['type'] == 'prompt']
        errors = [entry for entry in execution_log if entry['type'] == 'error']
        
        # Reconstruct terminal view based on algorithm type
        terminal_view = reconstruct_terminal_view(context, algorithm_type)
        
        # Generate HTML content for the PDF with the exact format from the example
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ 
                    font-family: Arial, sans-serif; 
                    margin: 20px; 
                    padding: 0;
                }}
                
                .title-container {{
                    border: 1px solid #b8daff;
                    border-radius: 5px;
                    background-color: #f0f8ff;
                    padding: 10px;
                    margin-bottom: 20px;
                    text-align: center;
                }}
                
                .program-title {{ 
                    font-size: 24px;
                    font-weight: bold;
                    color: #0066cc; 
                    margin: 0;
                }}
                
                .content-container {{
                    display: flex;
                    border: 1px solid #ddd;
                    border-radius: 5px;
                    overflow: hidden;
                }}
                
                .left-column {{
                    width: 50%;
                    border-right: 1px solid #b8daff;
                    box-sizing: border-box;
                }}
                
                .right-column {{
                    width: 50%;
                    box-sizing: border-box;
                }}
                
                .column-header {{
                    color: #0066cc;
                    font-size: 16px;
                    font-weight: bold;
                    padding: 10px;
                    margin: 0;
                }}
                
                .code-section {{
                    background-color: #f8f8ff;
                    padding: 15px;
                    font-family: Consolas, Monaco, 'Courier New', monospace;
                    font-size: 14px;
                    line-height: 1.4;
                    white-space: pre-wrap;
                    overflow-wrap: break-word;
                }}
                
                .output-section {{
                    background-color: #f5f5f5;
                    padding: 15px;
                    font-family: Consolas, Monaco, 'Courier New', monospace;
                    font-size: 14px;
                    line-height: 1.4;
                }}
                
                .system-messages {{
                    margin-top: 20px;
                    border-top: 1px solid #eee;
                    padding-top: 10px;
                }}
                
                .system-message-header {{
                    color: #0066cc;
                    font-size: 16px;
                    font-weight: bold;
                    margin-bottom: 10px;
                }}
                
                .system-message-box {{
                    background-color: #f9f9f9;
                    padding: 10px;
                    margin: 5px 0;
                    border-left: 3px solid #0066cc;
                }}
                
                .timestamp {{
                    color: #666;
                    font-size: 0.9em;
                }}
                
                /* Remove any table borders */
                table {{ 
                    border-collapse: collapse; 
                    width: 100%; 
                    margin: 0; 
                    border: none; 
                }}
                
                th, td {{ 
                    padding: 8px; 
                    text-align: left; 
                    border: none; 
                }}
            </style>
        </head>
        <body>
            <div class="title-container">
                <div class="program-title">{html.escape(program_title)}</div>
            </div>
            
            <div class="content-container">
                <div class="left-column">
                    <div class="column-header">Source Code</div>
                    <div class="code-section">{html.escape(code)}</div>
                </div>
                
                <div class="right-column">
                    <div class="column-header">OUTPUT</div>
                    <div class="output-section">
                        {terminal_view}
                    </div>
                </div>
            </div>
            
            <div class="system-messages">
                <div class="system-message-header">System Messages</div>
                {generate_system_messages_html(filtered_execution_log)}
            </div>
        </body>
        </html>
        """
        
        with open("output.html", "w") as file:
            file.write(html_content)
        
        # Check if wkhtmltopdf is installed
        try:
            subprocess.run(["which", "wkhtmltopdf"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # Install wkhtmltopdf if not available
            await update.message.reply_text("Installing PDF generation tool...")
            subprocess.run(["apt-get", "update"], check=True)
            subprocess.run(["apt-get", "install", "-y", "wkhtmltopdf"], check=True)
        
        # Generate PDF with proper page size and margins
        subprocess.run([
            "wkhtmltopdf",
            "--page-size", "A4",
            "--margin-top", "15",
            "--margin-bottom", "15",
            "--margin-left", "15",
            "--margin-right", "15",
            "output.html", "output.pdf"
        ])
        
        # Send PDF to user
        with open('output.pdf', 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=f"{program_title.replace(' ', '_')}.pdf",
                caption=f"Here's the execution report of your C code: {program_title}"
            )
        
        # Also send HTML file for better viewing
        with open('output.html', 'rb') as html_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=html_file,
                filename=f"{program_title.replace(' ', '_')}.html",
                caption="HTML version of the execution report for better viewing."
            )
    except Exception as e:
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)

def reconstruct_terminal_view(context, algorithm_type):
    """Reconstruct the terminal view from execution log based on algorithm type."""
    execution_log = context.user_data['execution_log']
    
    # Extract all prompts and outputs in order
    terminal_entries = []
    for entry in execution_log:
        if entry['type'] in ['prompt', 'output', 'input']:
            terminal_entries.append(entry)
    
    # Sort by timestamp
    terminal_entries.sort(key=lambda x: x['timestamp'])
    
    # Process based on algorithm type
    if algorithm_type in ["FCFS", "SJF", "Priority", "Round Robin"]:
        return reconstruct_cpu_scheduling_view(terminal_entries)
    elif algorithm_type in ["Best Fit", "Worst Fit", "First Fit", "Memory Management"]:
        return reconstruct_memory_management_view(terminal_entries)
    else:
        # Generic terminal view for other programs
        return reconstruct_generic_terminal_view(terminal_entries)

def reconstruct_cpu_scheduling_view(terminal_entries):
    """Reconstruct terminal view for CPU scheduling algorithms."""
    html_output = []
    
    # Extract key information
    num_processes = None
    process_data = []
    
    # First pass: extract process count and burst times
    for entry in terminal_entries:
        message = entry['message']
        
        # Look for number of processes
        match = re.search(r'Enter the (?:no\.?|number) of process(?:es)?\s*:\s*(\d+)', message, re.IGNORECASE)
        if match and num_processes is None:
            num_processes = int(match.group(1))
            html_output.append(f"<p>Enter the number of blocks : {num_processes}</p>")
            continue
        
        # Look for burst times
        match = re.search(r'Enter the [Bb]urst time of process\s*(\d+)\s*:\s*(\d+)', message)
        if match:
            process_id = int(match.group(1))
            burst_time = int(match.group(2))
            
            # Add to process data
            while len(process_data) <= process_id:
                process_data.append({})
            
            process_data[process_id]['burst'] = burst_time
            html_output.append(f"<p>Block {process_id+1} : {burst_time}</p>")
            continue
    
    # If we found processes, add the rest of the output
    if num_processes and process_data:
        # Add number of files (same as processes for CPU scheduling)
        html_output.insert(1, f"<p>Enter the number of files : {num_processes}</p>")
        
        # Add file sizes section header
        html_output.append("<p>Enter the size of the files :-</p>")
        
        # Add file sizes (same as burst times for CPU scheduling)
        for i, proc in enumerate(process_data):
            if 'burst' in proc:
                html_output.append(f"<p>File {i+1}: {proc['burst']}</p>")
        
        # Look for allocation results
        for entry in terminal_entries:
            message = entry['message']
            
            # Look for turnaround and waiting times in table format
            if re.search(r'\d+\s+\d+\s+\d+\s+\d+', message):
                parts = re.split(r'\s+', message.strip())
                if len(parts) >= 4:
                    try:
                        pid = int(parts[0])
                        burst = int(parts[1])
                        turnaround = int(parts[2])
                        waiting = int(parts[3])
                        
                        # Add allocation message based on waiting time
                        if waiting > 0:
                            html_output.append(f"<p>File Size {burst} is put in {turnaround} partition</p>")
                        else:
                            html_output.append(f"<p>File Size {burst} must wait</p>")
                    except (ValueError, IndexError):
                        pass
    else:
        # If we couldn't extract structured data, just show all terminal output
        for entry in terminal_entries:
            html_output.append(f"<p>{html.escape(entry['message'])}</p>")
    
    return "\n".join(html_output)

def reconstruct_memory_management_view(terminal_entries):
    """Reconstruct terminal view for memory management algorithms."""
    html_output = []
    
    # Extract key information
    num_blocks = None
    num_files = None
    blocks = []
    files = []
    
    # First pass: extract blocks and files
    for entry in terminal_entries:
        message = entry['message']
        
        # Look for number of blocks
        match = re.search(r'Enter the (?:no\.?|number) of blocks\s*:\s*(\d+)', message, re.IGNORECASE)
        if match and num_blocks is None:
            num_blocks = int(match.group(1))
            html_output.append(f"<p>Enter the number of blocks : {num_blocks}</p>")
            continue
        
        # Look for number of files
        match = re.search(r'Enter the (?:no\.?|number) of files\s*:\s*(\d+)', message, re.IGNORECASE)
        if match and num_files is None:
            num_files = int(match.group(1))
            html_output.append(f"<p>Enter the number of files : {num_files}</p>")
            continue
        
        # Look for block sizes
        match = re.search(r'Block\s*(\d+)\s*:\s*(\d+)', message, re.IGNORECASE)
        if match:
            block_id = int(match.group(1))
            block_size = int(match.group(2))
            
            # Add to blocks list
            while len(blocks) < block_id:
                blocks.append(0)
            
            blocks.append(block_size)
            continue
        
        # Look for file sizes
        match = re.search(r'File\s*(\d+)\s*:\s*(\d+)', message, re.IGNORECASE)
        if match:
            file_id = int(match.group(1))
            file_size = int(match.group(2))
            
            # Add to files list
            while len(files) < file_id:
                files.append(0)
            
            files.append(file_size)
            continue
    
    # If we found blocks and files, format the output
    if blocks and files:
        # Add block sizes section header
        if "Enter the size of the blocks" not in html_output[2]:
            html_output.append("<p>Enter the size of the blocks :</p>")
        
        # Add block sizes
        for i, size in enumerate(blocks):
            if size > 0:
                html_output.append(f"<p>Block {i} : {size}</p>")
        
        # Add file sizes section header
        html_output.append("<p>Enter the size of the files :-</p>")
        
        # Add file sizes
        for i, size in enumerate(files):
            if size > 0:
                html_output.append(f"<p>File {i}: {size}</p>")
        
        # Look for allocation results
        for entry in terminal_entries:
            message = entry['message']
            
            # Look for "is put in" messages
            match = re.search(r'File Size\s*(\d+)\s*is put in\s*(\d+)\s*partition', message, re.IGNORECASE)
            if match:
                file_size = match.group(1)
                partition = match.group(2)
                html_output.append(f"<p>File Size {file_size} is put in {partition} partition</p>")
                continue
            
            # Look for "must wait" messages
            match = re.search(r'File Size\s*(\d+)\s*must wait', message, re.IGNORECASE)
            if match:
                file_size = match.group(1)
                html_output.append(f"<p>File Size {file_size} must wait</p>")
                continue
    else:
        # If we couldn't extract structured data, just show all terminal output
        for entry in terminal_entries:
            html_output.append(f"<p>{html.escape(entry['message'])}</p>")
    
    return "\n".join(html_output)

def reconstruct_generic_terminal_view(terminal_entries):
    """Reconstruct terminal view for generic programs."""
    html_output = []
    
    for entry in terminal_entries:
        # Skip user inputs to avoid duplication
        if entry['type'] == 'input':
            continue
            
        html_output.append(f"<p>{html.escape(entry['message'])}</p>")
    
    if not html_output:
        return "<p>No terminal output available</p>"
    
    return "\n".join(html_output)

def generate_system_messages_html(system_messages):
    """Generate HTML for system messages section."""
    if not system_messages:
        return "<p>No system messages</p>"
    
    html_output = ""
    
    for msg in system_messages:
        timestamp = msg['timestamp'].strftime("%H:%M:%S.%f")[:-3]
        html_output += f"""
        <div class="system-message-box">
            <span class="timestamp">[{timestamp}]</span> <strong>System:</strong>
            <p>{msg['message']}</p>
        </div>
        """
    
    return html_output

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        process.terminate()
        try:
            await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    
    for file in ["temp.c", "temp", "output.pdf", "output.html"]:
        if os.path.exists(file):
            os.remove(file)
    
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

def main() -> None:
    try:
        # Build the application
        application = Application.builder().token(TOKEN).build()
        
        # Define the conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
                RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
                TITLE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_input)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )
        
        # Add handler to application
        application.add_handler(conv_handler)
        
        # Log startup and start polling
        logger.info("Bot is about to start polling with token: %s", TOKEN[:10] + "...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except telegram.error.Conflict as e:
        logger.error(f"Conflict error: {e}. Ensure only one bot instance is running.")
        print("Error: Another instance of this bot is already running. Please stop it and try again.")
        return
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise

if __name__ == '__main__':
    main()
