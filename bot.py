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
CODE, RUNNING = range(2)

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
                    
                    await update.message.reply_text("Program execution completed.")
                    await generate_and_send_pdf(update, context)
                    break
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
            
            await update.message.reply_text("Program execution completed.")
            await generate_and_send_pdf(update, context)
            break

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

    if not process or process.returncode is not None:
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
        await generate_and_send_pdf(update, context)
        return ConversationHandler.END
    
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

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        
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
        
        # Reconstruct terminal view
        terminal_view = reconstruct_terminal_view(context)
        
        # Generate HTML content for the PDF
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #333; }}
                pre {{ background-color: #f5f5f5; padding: 10px; border-radius: 5px; overflow-x: auto; }}
                .terminal {{ background-color: #f0f0f0; padding: 15px; border-radius: 5px; font-family: monospace; }}
                .system-message {{ color: #0066cc; }}
                .input {{ color: #009900; }}
                .output {{ color: #000000; }}
                .prompt {{ color: #990000; }}
                .error {{ color: #cc0000; }}
                
                /* Modified table styles to remove borders between rows */
                table {{ 
                    border-collapse: collapse; 
                    width: 100%; 
                    margin: 15px 0; 
                    border: none; 
                }}
                
                th {{ 
                    background-color: #f2f2f2; 
                    padding: 8px; 
                    text-align: left; 
                    border: none; 
                }}
                
                td {{ 
                    padding: 8px; 
                    text-align: left; 
                    border: none; 
                }}
                
                /* Add subtle background to alternate rows for readability */
                tr:nth-child(even) {{ 
                    background-color: #f9f9f9; 
                }}
                
                /* Remove progress bars completely */
                .progress-container, .progress-bar {{ 
                    display: none; 
                }}
                
                .terminal-view {{ 
                    background-color: #f5f5f5; 
                    padding: 15px; 
                    border-radius: 5px; 
                    font-family: monospace; 
                }}
                
                .system-messages {{ 
                    margin-top: 20px; 
                    border-top: 1px solid #eee; 
                    padding-top: 10px; 
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
            </style>
        </head>
        <body>
            <h1>Source Code</h1>
            <pre><code>{html.escape(code)}</code></pre>
            
            <h1>Terminal View</h1>
            <div class="terminal-view">
                {terminal_view}
            </div>
            
            <h1>System Messages</h1>
            <div class="system-messages">
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
        
        # Generate PDF
        subprocess.run(["wkhtmltopdf", "output.html", "output.pdf"])
        
        # Send PDF to user
        with open('output.pdf', 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename="program_execution.pdf",
                caption="Here's the execution report of your C code."
            )
        
        # Also send HTML file for better viewing
        with open('output.html', 'rb') as html_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=html_file,
                filename="program_execution.html",
                caption="HTML version of the execution report for better viewing."
            )
    except Exception as e:
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)

def reconstruct_terminal_view(context):
    """Reconstruct the terminal view from execution log."""
    execution_log = context.user_data['execution_log']
    
    # Extract process data from execution log
    process_data = extract_process_data_from_log(execution_log)
    
    if not process_data:
        return "<p>No process data available</p>"
    
    # Generate the terminal view HTML
    html_output = ""
    
    # First, add the process input section
    num_processes = len(process_data)
    html_output += f"<p>Enter the no.of process: {num_processes}</p>"
    
    for i, proc in enumerate(process_data):
        html_output += f"<p>Enter the Burst time of process {i} : {proc['burst']}</p>"
    
    # Add order of execution
    html_output += "<p>Order of execution:</p>"
    execution_order = "P0"
    for i in range(1, num_processes):
        execution_order += f"->P{i}"
    html_output += f"<p>{execution_order}-></p>"
    
    # Add the process table header with clean styling (no borders)
    html_output += """
    <table>
        <tr>
            <th>PID</th>
            <th>Burst Time</th>
            <th>Turnaround Time</th>
            <th>Waiting Time</th>
        </tr>
    """
    
    # Add each process row without any progress bars or borders
    for proc in process_data:
        html_output += f"""
        <tr>
            <td>{proc['pid']}</td>
            <td>{proc['burst']}</td>
            <td>{proc['turnaround']}</td>
            <td>{proc['waiting']}</td>
        </tr>
        """
    
    html_output += "</table>"
    
    return html_output

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

def extract_process_data_from_log(execution_log):
    """Extract process scheduling data from execution log if available."""
    processes = []
    
    # Look for patterns in output that might indicate process data
    pid_pattern = re.compile(r'Enter the Burst time of process (\d+)\s*:\s*(\d+)')
    
    for entry in execution_log:
        if entry['type'] == 'output' or entry['type'] == 'prompt':
            match = pid_pattern.search(entry['message'])
            if match:
                pid = int(match.group(1))
                burst = int(match.group(2))
                
                # Check if this process is already in our list
                existing = next((p for p in processes if p['pid'] == pid), None)
                if existing:
                    existing['burst'] = burst
                else:
                    processes.append({
                        'pid': pid,
                        'burst': burst,
                        'turnaround': 0,
                        'waiting': 0
                    })
    
    # If we found processes, try to extract turnaround and waiting times
    if processes:
        # Sort by PID
        processes.sort(key=lambda x: x['pid'])
        
        # Look for turnaround and waiting time patterns
        for entry in execution_log:
            if entry['type'] == 'output':
                # Try to match lines like "0       21      21      0"
                parts = entry['message'].strip().split()
                if len(parts) == 4:
                    try:
                        pid = int(parts[0])
                        burst = int(parts[1])
                        turnaround = int(parts[2])
                        waiting = int(parts[3])
                        
                        # Find the process with this PID
                        proc = next((p for p in processes if p['pid'] == pid), None)
                        if proc:
                            proc['burst'] = burst
                            proc['turnaround'] = turnaround
                            proc['waiting'] = waiting
                    except (ValueError, IndexError):
                        pass
    
    return processes

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
