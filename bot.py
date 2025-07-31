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
    """Receives, cleans, and compiles the C code."""
    original_code = update.message.text
    code = clean_whitespace(original_code)

    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code."
        )

    context.user_data.update({
        'code': code, 'output': [], 'inputs': [], 'errors': [],
        'execution_log': [], 'terminal_log': []
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
            # Directly call the function to handle the running process
            return await handle_running_logic(update, context)
        else:
            error_message = compile_result.stderr
            await update.message.reply_text(f"Compilation Error:\n{error_message}")
            return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")
        return ConversationHandler.END

async def read_from_stream(stream):
    """Reads from a stream without blocking indefinitely."""
    buffer = b''
    while True:
        try:
            # Read with a very short timeout to avoid blocking
            chunk = await asyncio.wait_for(stream.read(1024), timeout=0.1)
            if not chunk:
                break
            buffer += chunk
        except asyncio.TimeoutError:
            break
    return buffer.decode()

async def handle_running_logic(update: Update, context: CallbackContext):
    """Manages the I/O loop for the running C program."""
    process = context.user_data['process']

    # Initial read of output before asking for any input
    output = await read_from_stream(process.stdout)
    if output:
        await update.message.reply_text(f"Program output:\n{output}")
        context.user_data['terminal_log'].append({'type': 'output', 'content': output, 'timestamp': datetime.datetime.now()})

    # Check if the process has already terminated
    if process.returncode is not None:
        await update.message.reply_text("Program execution completed.")
        return await ask_for_title(update, context)

    # If the process is still running, it's likely waiting for input
    await update.message.reply_text("Please provide input, or type 'done' to finish.")
    return RUNNING


async def handle_running(update: Update, context: CallbackContext) -> int:
    """Handles user input while the C program is running."""
    user_input = update.message.text
    process = context.user_data.get('process')

    if not process or process.returncode is not None:
        await update.message.reply_text("The program is no longer running.")
        return ConversationHandler.END

    if user_input.lower() == 'done':
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        await process.wait()
        await update.message.reply_text("Program terminated by user.")
        return await ask_for_title(update, context)

    # Log and send the user's input
    context.user_data['terminal_log'].append({
        'type': 'input', 'content': user_input + "\n",
        'timestamp': datetime.datetime.now()
    })
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    await update.message.reply_text(f"Input sent: {user_input}")

    # After sending input, immediately try to read the next batch of output
    return await handle_running_logic(update, context)


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
        terminal_log = sorted(context.user_data.get('terminal_log', []), key=lambda x: x['timestamp'])

        # Build the terminal view content
        terminal_content = ""
        for entry in terminal_log:
            content_type = entry['type']
            escaped_content = html.escape(entry['content'])
            if content_type == 'input':
                terminal_content += f"<strong><pre>> {escaped_content}</pre></strong>"
            else:
                terminal_content += f"<pre>{escaped_content}</pre>"
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>{html.escape(title)} - C Program Execution Report</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 20px; line-height: 1.6; }}
                h1, h2 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
                pre {{ background-color: #f4f4f4; padding: 15px; border-radius: 5px; white-space: pre-wrap; word-wrap: break-word; }}
                code {{ font-family: "Courier New", Courier, monospace; }}
                .terminal {{ background-color: #2b2b2b; color: #f8f8f2; padding: 20px; border-radius: 8px; }}
                .terminal strong pre {{ color: #2ecc71; }} /* Green for input */
            </style>
        </head>
        <body>
            <h1>{html.escape(title)}</h1>
            <h2>Source Code</h2>
            <pre><code>{html.escape(code)}</code></pre>
            <h2>Execution Log</h2>
            <div class="terminal">{terminal_content}</div>
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
            await context.bot.send_document(update.effective_chat.id, document=open('output.html', 'rb'), filename="execution_report.html")
        else:
            await update.message.reply_text("Generating execution report...")
            pdf_filename = f"{title.replace(' ', '_').lower()}.pdf"
            html_filename = f"{title.replace(' ', '_').lower()}.html"
            await context.bot.send_document(update.effective_chat.id, document=open('output.pdf', 'rb'), filename=pdf_filename)
            await context.bot.send_document(update.effective_chat.id, document=open('output.html', 'rb'), filename=html_filename)

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
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError) as e:
            logger.warning(f"Could not terminate process gracefully, killing. Reason: {e}")
            if process.returncode is None:
                process.kill()
    
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
        logger.error("Conflict error. Another bot instance is already running.")
        print("Error: Another instance of the bot is already running. Please stop it and try again.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        raise

if __name__ == '__main__':
    main()
