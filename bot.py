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
    cleaned_code = code.replace('\u00A0', ' ')
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    original_code = update.message.text
    code = clean_whitespace(original_code)
    
    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code that would cause compilation errors."
        )
    
    context.user_data['code'] = code
    context.user_data['output'] = []
    context.user_data['inputs'] = []
    context.user_data['errors'] = []
    context.user_data['waiting_for_input'] = False
    context.user_data['execution_log'] = []
    context.user_data['output_buffer'] = ""
    context.user_data['terminal_log'] = []
    context.user_data['program_completed'] = False
    
    try:
        with open("temp.c", "w") as file:
            file.write(code)
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
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
            
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            if "stray" in compile_result.stderr and "\\302" in compile_result.stderr:
                code = re.sub(r'[^\x00-\x7F]+', ' ', code)
                
                with open("temp.c", "w") as file:
                    file.write(code)
                
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
                    
                    asyncio.create_task(read_process_output(update, context))
                    return RUNNING
            
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Compilation Error:\n{compile_result.stderr}",
                'timestamp': datetime.datetime.now()
            })
            
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
    
    output_seen = False
    read_size = 1024
    
    while True:
        stdout_task = asyncio.create_task(process.stdout.read(read_size))
        stderr_task = asyncio.create_task(process.stderr.read(read_size))
        
        done, pending = await asyncio.wait(
            [stdout_task, stderr_task],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.5
        )

        if not done:
            for task in pending:
                task.cancel()
            
            if process.returncode is not None:
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                if output_seen:
                    execution_log.append({
                        'type': 'system',
                        'message': 'Program execution completed.',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    context.user_data['program_completed'] = True
                    
                    await update.message.reply_text("Program execution completed.")
                    
                    await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                    return TITLE_INPUT
                else:
                    await asyncio.sleep(0.1)
                    continue
            
            if output_seen and not context.user_data.get('waiting_for_input', False):
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                context.user_data['waiting_for_input'] = True
                
                last_prompt = "unknown"
                for entry in reversed(execution_log):
                    if entry['type'] == 'prompt':
                        last_prompt = entry['message']
                        break
                
                input_message = f"Program is waiting for input: \"{last_prompt}\"\nPlease provide input (or type 'done' to finish):"
                
                execution_log.append({
                    'type': 'system',
                    'message': input_message,
                    'timestamp': datetime.datetime.now()
                })
                await update.message.reply_text(input_message)
            
            continue

        if stdout_task in done:
            stdout_chunk = await stdout_task
            if stdout_chunk:
                decoded_chunk = stdout_chunk.decode()
                output_seen = True
                
                terminal_log.append(decoded_chunk)
                
                output_buffer += decoded_chunk
                context.user_data['output_buffer'] = output_buffer
                
                output_buffer = process_output_chunk(context, output_buffer, update)
                context.user_data['output_buffer'] = output_buffer

        if stderr_task in done:
            stderr_chunk = await stderr_task
            if stderr_chunk:
                decoded_chunk = stderr_chunk.decode()
                
                for line in decoded_chunk.splitlines(True):
                    errors.append(line.strip())
                    
                    execution_log.append({
                        'type': 'error',
                        'message': line.strip(),
                        'timestamp': datetime.datetime.now(),
                        'raw': line
                    })
                    
                    await update.message.reply_text(f"Error: {line.strip()}")

        for task in pending:
            task.cancel()

        if process.returncode is not None:
            if output_buffer:
                process_output_chunk(context, output_buffer, update)
            
            execution_log.append({
                'type': 'system',
                'message': 'Program execution completed.',
                'timestamp': datetime.datetime.now()
            })
            
            context.user_data['program_completed'] = True
            
            await update.message.reply_text("Program execution completed.")
            
            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
            return TITLE_INPUT

def process_output_chunk(context, buffer, update):
    """Process the output buffer, preserving tabs and whitespace."""
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    
    lines = re.findall(r'[^\n]*\n|[^\n]+$', buffer)
    
    new_buffer = ""
    if lines and not buffer.endswith('\n'):
        new_buffer = lines[-1]
        lines = lines[:-1]
    
    for line in lines:
        line_stripped = line.strip()
        if line_stripped:
            output.append(line_stripped)
            
            is_prompt = (
                line_stripped.rstrip().endswith((':','>','?')) or
                re.search(r'(Enter|Input|Type|Provide|Give)(\s|\w)*', line_stripped, re.IGNORECASE) or
                "number" in line_stripped.lower()
            )
            
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line
            }
            
            execution_log.append(log_entry)
            
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(update.message.reply_text(f"{prefix} {line_stripped}"))
    
    return new_buffer

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    execution_log = context.user_data['execution_log']
    terminal_log = context.user_data['terminal_log']

    if context.user_data.get('program_completed', False):
        return await handle_title_input(update, context)

    if not process or process.returncode is not None:
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
        
        context.user_data['program_completed'] = True
        
        await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
        return TITLE_INPUT
    
    execution_log.append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    terminal_log.append(user_input + "\n")
    
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    
    await update.message.reply_text(f"Input sent: {user_input}")
    
    return RUNNING

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    
    if title.lower() == 'skip':
        context.user_data['program_title'] = "C Program Execution Report"
    else:
        context.user_data['program_title'] = title
    
    await update.message.reply_text(f"Using title: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")

        # Generate HTML with proper tab alignment styling
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .program-title {{
                    font-size: 30px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 20px;
                    text-decoration: underline;
                }}
                pre {{
                    font-family: 'Courier New', monospace;
                    white-space: pre;
                    font-size: 25px;
                    line-height: 1.3;
                    tab-size: 8;
                    -moz-tab-size: 8;
                    -o-tab-size: 8;
                    background: #FFFFFF;
                    padding: 10px;
                    border-radius: 5px;
                }}
                .terminal-view {{
                    margin: 15px 0;
                }}
            </style>
        </head>
        <body>
            <div class="program-title">{html.escape(program_title)}</div>
            
            <h1><u><strong>SOURCE CODE</strong></u></h1>
            <pre><code>{html.escape(code)}</code></pre>
            
            <h1><u><strong>OUTPUT</strong></u></h1>
            <div class="terminal-view">
                {reconstruct_terminal_view(context)}
            </div>
        </body>
        </html>
        """

        with open("output.html", "w") as file:
            file.write(html_content)

        # Generate sanitized filename from title
        # Replace invalid filename characters with underscores and ensure it ends with .pdf
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)  # Replace spaces with underscores
        pdf_filename = f"{sanitized_title}.pdf"
        
        # Generate PDF
        subprocess.run(["wkhtmltopdf", "output.html", pdf_filename])

        # Send PDF to user
        with open(pdf_filename, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=pdf_filename,
                caption=f"Execution report for {program_title}"
            )

    except Exception as e:
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)


def reconstruct_terminal_view(context):
    """Preserve exact terminal formatting with tabs"""
    terminal_log = context.user_data.get('terminal_log', [])
    
    if terminal_log:
        raw_output = ''.join(terminal_log)
        # Double tab width for better PDF readability while maintaining alignment
        raw_output = raw_output.expandtabs(12)  # 12 spaces per tab
        return f"""
        <div style="
            font-family: 'Courier New', monospace;
            white-space: pre;
            font-size: 12px;
            line-height: 1.2;
            background: #f8f8f8;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
        ">{html.escape(raw_output)}</div>
        """
    
    return "<pre>No terminal output available</pre>"
    
    # Otherwise try to reconstruct from execution log
    if execution_log:
        output_lines = []
        for entry in execution_log:
            if entry['type'] in ['output', 'prompt', 'error']:
                # Get raw output if available, otherwise use message
                line = entry.get('raw', entry['message'])
                # Replace tabs with spaces and preserve all whitespace
                line = line.replace('\t', '    ')
                output_lines.append(line)
        
        # Join all lines and wrap in <pre> tag to preserve formatting
        formatted_output = f"<pre>{html.escape(''.join(output_lines))}</pre>"
        return formatted_output
    
    return "<pre>No terminal output available</pre>"

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
    
    for file in ["temp.c", "temp", "output.html"]:
        if os.path.exists(file):
            os.remove(file)
    
    for file in os.listdir():
        if file.endswith(".pdf") and file != "bot.py" and file != "modified_bot.py":
            os.remove(file)
    
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

def main() -> None:
    try:
        application = Application.builder().token(TOKEN).build()
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
                RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
                TITLE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_input)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )
        
        application.add_handler(conv_handler)
        
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
