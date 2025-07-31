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
CODE, RUNNING, ASK_TITLE = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    """Starts the conversation and asks for C code."""
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.'
    )
    return CODE

def clean_whitespace(code: str) -> str:
    """Cleans non-standard whitespace characters from the code."""
    cleaned_code = code.replace('\u00A0', ' ')
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    cleaned_code = cleaned_code.replace('\t', '    ')
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    """Receives and compiles the C code."""
    original_code = update.message.text
    code = clean_whitespace(original_code)

    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code."
        )

    context.user_data.update({
        'code': code, 'output': [], 'inputs': [], 'errors': [],
        'waiting_for_input': False, 'execution_log': [],
        'output_buffer': "", 'terminal_log': []
    })

    try:
        with open("temp.c", "w") as file:
            file.write(code)

        compile_result = subprocess.run(
            ["gcc", "temp.c", "-o", "temp"],
            capture_output=True, text=True
        )

        if compile_result.returncode == 0:
            context.user_data['execution_log'].append({
                'type': 'system', 'message': 'Code compiled successfully!',
                'timestamp': datetime.datetime.now()
            })
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "./temp",
                stdin=PIPE, stdout=PIPE, stderr=PIPE
            )
            context.user_data['process'] = process
            await update.message.reply_text("Code compiled successfully! Running now...")
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            error_message = compile_result.stderr
            if "stray" in error_message and ("\\302" in error_message or "\\240" in error_message):
                await update.message.reply_text(
                    f"Compilation Error (non-standard whitespace):\n{error_message}\n\n"
                    "Your code may contain invisible non-standard whitespace. Please try retyping the code."
                )
            else:
                await update.message.reply_text(f"Compilation Error:\n{error_message}")
            return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")
        return ConversationHandler.END

async def read_process_output(update: Update, context: CallbackContext):
    """Reads and handles the output of the running C program."""
    process = context.user_data['process']
    output_seen = False

    while True:
        tasks = [
            asyncio.create_task(process.stdout.read(1024)),
            asyncio.create_task(process.stderr.read(1024))
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=0.5)

        for task in pending:
            task.cancel()

        if not done:
            if process.returncode is not None:
                if context.user_data.get('output_buffer'):
                    process_output_chunk(context, context.user_data['output_buffer'], update)
                context.user_data['execution_log'].append({
                    'type': 'system', 'message': 'Program execution completed.',
                    'timestamp': datetime.datetime.now()
                })
                await update.message.reply_text("Program execution completed.")
                await ask_for_title(update, context)
                return
            elif output_seen and not context.user_data.get('waiting_for_input'):
                context.user_data['waiting_for_input'] = True
                await update.message.reply_text("Program appears to be waiting for input. Type your input or 'done' to finish.")
            continue

        for task in done:
            if task == tasks[0]:  # stdout
                stdout_chunk = await task
                if stdout_chunk:
                    output_seen = True
                    decoded_chunk = stdout_chunk.decode()
                    context.user_data['terminal_log'].append({
                        'type': 'output', 'content': decoded_chunk.lstrip(),
                        'timestamp': datetime.datetime.now()
                    })
                    buffer = context.user_data.get('output_buffer', "") + decoded_chunk
                    context.user_data['output_buffer'] = process_output_chunk(context, buffer, update)
            elif task == tasks[1]:  # stderr
                stderr_chunk = await task
                if stderr_chunk:
                    await update.message.reply_text(f"Error: {stderr_chunk.decode().strip()}")

        if process.returncode is not None:
            break
    
    await ask_for_title(update, context)


def process_output_chunk(context: CallbackContext, buffer: str, update: Update) -> str:
    """Processes chunks of program output to display to the user."""
    lines = re.findall(r'[^\n]*\n|[^\n]+$', buffer)
    new_buffer = ""
    if lines and not buffer.endswith('\n'):
        new_buffer = lines.pop()

    for line in lines:
        line_stripped = line.strip()
        if line_stripped:
            is_prompt = line_stripped.rstrip().endswith((':','>','?')) or \
                        re.search(r'(Enter|Input|Type|Provide|Give)', line_stripped, re.IGNORECASE)
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(update.message.reply_text(f"{prefix} {line_stripped}"))
    return new_buffer

async def handle_running(update: Update, context: CallbackContext) -> int:
    """Handles user input while the C program is running."""
    user_input = update.message.text
    process = context.user_data.get('process')

    if not process or process.returncode is not None:
        await update.message.reply_text("The program is no longer running.")
        return ConversationHandler.END

    if user_input.lower() == 'done':
        process.stdin.close()
        await process.wait()
        await ask_for_title(update, context)
        return ASK_TITLE

    context.user_data['terminal_log'].append({
        'type': 'input', 'content': user_input + "\n",
        'timestamp': datetime.datetime.now()
    })
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['waiting_for_input'] = False
    await update.message.reply_text(f"Input sent: {user_input}")
    return RUNNING

async def ask_for_title(update: Update, context: CallbackContext) -> int:
    """Prompts the user to enter a title for the PDF report."""
    await update.message.reply_text("Please enter a title for the PDF report.")
    return ASK_TITLE

async def receive_title(update: Update, context: CallbackContext) -> int:
    """Receives the title and triggers the report generation."""
    title = update.message.text
    await generate_and_send_pdf(update, context, title)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext, title: str):
    """Generates and sends the PDF and HTML execution reports."""
    try:
        code = context.user_data['code']
        terminal_log = sorted(context.user_data['terminal_log'], key=lambda x: x['timestamp'])

        terminal_content = "".join(f'<span class="terminal-line">  {html.escape(entry["content"])}</span>' 
                                   for entry in terminal_log)

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>{html.escape(title)} - C Program Execution Report</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1, h2 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
                pre {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; white-space: pre-wrap; }}
                .terminal {{ background-color: #2b2b2b; color: #f8f8f2; font-family: monospace; }}
            </style>
        </head>
        <body>
            <h1>{html.escape(title)}</h1>
            <h2>Source Code</h2>
            <pre><code>{html.escape(code)}</code></pre>
            <h2>Terminal View</h2>
            <pre class="terminal">{terminal_content}</pre>
        </body>
        </html>
        """
        
        with open("output.html", "w") as file:
            file.write(html_content)

        pdf_process = subprocess.run(
            ["wkhtmltopdf", "--enable-local-file-access", "output.html", "output.pdf"],
            capture_output=True, text=True
        )

        if pdf_process.returncode != 0:
            logger.error(f"PDF generation failed: {pdf_process.stderr}")
            await update.message.reply_text("Failed to generate PDF. Sending HTML instead.")
            await context.bot.send_document(update.effective_chat.id, document=open('output.html', 'rb'), filename="program_execution.html")
        else:
            await update.message.reply_text("Generating execution report...")
            await context.bot.send_document(update.effective_chat.id, document=open('output.pdf', 'rb'), filename=f"{title.replace(' ', '_')}.pdf")
            await context.bot.send_document(update.effective_chat.id, document=open('output.html', 'rb'), filename=f"{title.replace(' ', '_')}.html")

    except Exception as e:
        logger.error(f"Error in report generation: {e}")
        await update.message.reply_text(f"Failed to generate report: {e}")
    finally:
        await cleanup(context)

async def cleanup(context: CallbackContext):
    """Cleans up temporary files and clears user data."""
    process = context.user_data.get('process')
    if process and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
        except Exception as e:
            logger.error(f"Error during process cleanup: {e}")
    
    for file in ["temp.c", "temp", "output.pdf", "output.html"]:
        if os.path.exists(file):
            os.remove(file)
            
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancels the current operation."""
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

def main() -> None:
    """Initializes and runs the bot."""
    try:
        application = Application.builder().token(TOKEN).build()
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
                RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
                ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )
        application.add_handler(conv_handler)
        logger.info("Bot is starting...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except telegram.error.Conflict:
        logger.error("Conflict error. Another bot instance is running.")
        print("Error: Another instance of the bot is already running. Please stop it and try again.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        raise

if __name__ == '__main__':
    main()
