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
    
    # Replace tabs with spaces (optional, but helps with consistency)
    cleaned_code = cleaned_code.replace('\t', '    ')
    
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
    context.user_data['terminal_log'] = []  # Specific log for terminal view
    
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
                    await generate_and_send_pdf(update, context, title="Program Execution Log")
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
                
                # Add to terminal log for clean terminal view
                # Trim any leading whitespace to prevent unwanted indentation
                terminal_log.append({
                    'type': 'output',
                    'content': decoded_chunk.lstrip(' '),
                    'timestamp': datetime.datetime.now()
                })
                
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
            await generate_and_send_pdf(update, context, title="Program Execution Log")
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
        await generate_and_send_pdf(update, context, title="Program Execution Log")
        return ConversationHandler.END
    
    # Add user input to execution log
    execution_log.append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    # Add to terminal log for clean terminal view
    terminal_log.append({
        'type': 'input',
        'content': user_input + "\n",
        'timestamp': datetime.datetime.now()
    })
    
    # Send input to process
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    
    # Acknowledge the input
    await update.message.reply_text(f"Input sent: {user_input}")
    
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext, title: str = "Execution Log"):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        
        # Sort execution log by timestamp to ensure correct order
        execution_log.sort(key=lambda x: x['timestamp'])
        terminal_log.sort(key=lambda x: x['timestamp'])
        
        # Filter execution log to keep only compilation success and program completion messages
        filtered_execution_log = [
            entry for entry in execution_log 
            if entry['type'] == 'system' and (
                entry['message'] == 'Code compiled successfully!' or 
                entry['message'] == 'Code compiled successfully after aggressive whitespace cleaning!' or
                entry['message'] == 'Program execution completed.'
            )
        ]
        
        # Create a more detailed HTML with syntax highlighting and better formatting
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
                        <title>{title} - C Program Execution Report</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
                h2 {{ color: #3498db; margin-top: 20px; }}
                pre {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; overflow-x: auto; }}
                code {{ font-family: Consolas, Monaco, 'Andale Mono', monospace; }}
                .output {{ background-color: #e8f4f8; padding: 10px; border-left: 4px solid #3498db; margin: 10px 0; white-space: pre-wrap; }}
                .prompt {{ background-color: #e8f4f8; padding: 10px; border-left: 4px solid #9b59b6; margin: 10px 0; white-space: pre-wrap; }}
                .input {{ background-color: #f0f7e6; padding: 10px; border-left: 4px solid #27ae60; margin: 10px 0; white-space: pre-wrap; }}
                .error {{ background-color: #fae5e5; padding: 10px; border-left: 4px solid #e74c3c; margin: 10px 0; white-space: pre-wrap; }}
                .system {{ background-color: #f5f5f5; padding: 10px; border-left: 4px solid #7f8c8d; margin: 10px 0; white-space: pre-wrap; }}
                .execution-flow {{ margin-top: 20px; }}
                .timestamp {{ color: #7f8c8d; font-size: 0.8em; }}
                .interaction {{ border: 1px solid #eee; margin-bottom: 15px; padding: 10px; border-radius: 5px; }}
                .terminal {{ 
                    background-color: #2b2b2b; 
                    color: #f8f8f2; 
                    padding: 20px; 
                    border-radius: 5px; 
                    font-family: monospace; 
                    white-space: pre; 
                    line-height: 1.5;
                    margin: 0;
                    padding-left: 0;
                }}
                .terminal-line {{
                    margin: 0;
                    padding-left: 10px;  /* Add consistent indentation to each line */
                }}
            </style>
        </head>
        <body>
            <h1>{title}</h1>
            
            <h2>Source Code</h2>
            <pre><code>{html.escape(code)}</code></pre>
            
            <h2>Terminal View</h2>
            <pre class="terminal">"""
        
        # Create a clean terminal view that focuses on program prompts and user inputs
        terminal_content = ""
        for entry in terminal_log:
            entry_type = entry['type']
            content = entry['content']
            
            # Process the content line by line to add indentation to each line
            lines = content.splitlines(True)  # Keep line endings
            for line in lines:
                if line.strip():  # Only process non-empty lines
                    # Add a consistent indentation to each line
                    terminal_content += f'<span class="terminal-line">  {html.escape(line)}</span>'
                else:
                    terminal_content += html.escape(line)
        
        html_content += terminal_content
        
        html_content += """</pre>
        """
        
        # Add only the system messages for compilation success and program completion
        if filtered_execution_log:
            html_content += """
            <h2>System Messages</h2>
            <div class="execution-flow">
            """
            
            for entry in filtered_execution_log:
                timestamp = entry['timestamp'].strftime('%H:%M:%S.%f')[:-3]  # Include milliseconds
                html_content += f'<div class="system"><span class="timestamp">[{timestamp}]</span> <strong>System:</strong> <pre>{html.escape(entry["message"])}</pre></div>\n'
            
            html_content += """
            </div>
            """
        
        html_content += """
        </body>
        </html>
        """
        
        with open("output.html", "w") as file:
            file.write(html_content)
        
        # Generate PDF with wkhtmltopdf
        pdf_process = subprocess.run(
            ["wkhtmltopdf", "--enable-local-file-access", "output.html", "output.pdf"],
            capture_output=True,
            text=True
        )
        
        if pdf_process.returncode != 0:
            logger.error(f"PDF generation failed: {pdf_process.stderr}")
            await update.message.reply_text("Failed to generate PDF report. Sending HTML instead.")
            with open('output.html', 'rb') as html_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=html_file,
                    filename="program_execution.html"
                )
        else:
            # Send both PDF and HTML for maximum compatibility
            await update.message.reply_text("Generating execution report...")
            with open('output.pdf', 'rb') as pdf_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=pdf_file,
                    filename="program_execution.pdf"
                )
            
            with open('output.html', 'rb') as html_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=html_file,
                    filename="program_execution.html"
                )
        
    except Exception as e:
        logger.error(f"Error in PDF generation: {str(e)}")
        await update.message.reply_text(f"Failed to generate report: {str(e)}")
    finally:
        await cleanup(context)

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except Exception as e:
            logger.error(f"Error during process cleanup: {str(e)}")
    
    # Clean up temporary files
    for file in ["temp.c", "temp", "output.pdf", "output.html"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except Exception as e:
                logger.error(f"Error removing file {file}: {str(e)}")
    
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
