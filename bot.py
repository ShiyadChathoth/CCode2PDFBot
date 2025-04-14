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
import datetime
import re
import unicodedata
import sys

# Enhanced logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot_debug.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Telegram Bot Token
TOKEN = os.getenv('TOKEN')
if not TOKEN:
    logger.error("No TELEGRAM_BOT_TOKEN found in environment variables!")
    sys.exit(1)

# States for ConversationHandler
CODE, RUNNING, TITLE_INPUT = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.'
    )
    return CODE

def clean_whitespace(code):
    """Clean non-standard whitespace characters from code."""
    return ''.join(char if not unicodedata.category(char).startswith('Z') or char == ' ' else ' ' 
                  for char in code)

async def handle_code(update: Update, context: CallbackContext) -> int:
    try:
        original_code = update.message.text
        code = clean_whitespace(original_code)
        
        if code != original_code:
            await update.message.reply_text(
                "⚠️ I detected and fixed non-standard whitespace characters in your code."
            )
        
        context.user_data['code'] = code
        context.user_data['terminal_log'] = []
        
        with open("temp.c", "w") as file:
            file.write(code)
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
            process = await asyncio.create_subprocess_exec(
                "./temp",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE
            )
            
            context.user_data['process'] = process
            await update.message.reply_text("Code compiled successfully! Running now...")
            
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            await update.message.reply_text(f"Compilation Error:\n{compile_result.stderr}")
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"Error in handle_code: {str(e)}")
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    terminal_log = context.user_data['terminal_log']
    
    while True:
        stdout_data = await process.stdout.read(1024)
        stderr_data = await process.stderr.read(1024)
        
        if stdout_data:
            output = stdout_data.decode()
            terminal_log.append(output)
            await update.message.reply_text(f"Output:\n{output}")
            
        if stderr_data:
            error = stderr_data.decode()
            terminal_log.append(f"ERROR: {error}")
            await update.message.reply_text(f"Error:\n{error}")
            
        if process.returncode is not None:
            terminal_log.append(f"Process exited with code {process.returncode}")
            await update.message.reply_text("Program execution completed. Please provide a title for your program:")
            return TITLE_INPUT
            
        await asyncio.sleep(0.1)

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if process and process.returncode is None:
        try:
            process.stdin.write(f"{user_input}\n".encode())
            await process.stdin.drain()
            await update.message.reply_text(f"Input sent: {user_input}")
            return RUNNING
        except Exception as e:
            await update.message.reply_text(f"Failed to send input: {str(e)}")
            return ConversationHandler.END
    else:
        await update.message.reply_text("Program is not running. Send /start to begin again.")
        return ConversationHandler.END

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    context.user_data['program_title'] = title if title.lower() != 'skip' else "C Program Execution"
    
    await update.message.reply_text(f"Title set to: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data.get('code', 'No code provided')
        terminal_log = context.user_data.get('terminal_log', [])
        title = context.user_data.get('program_title', "C Program Execution")
        
        # Generate HTML
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .title {{ font-size: 24px; font-weight: bold; margin-bottom: 20px; }}
                pre {{ background: #f5f5f5; padding: 10px; border-radius: 5px; }}
            </style>
        </head>
        <body>
            <div class="title">{html.escape(title)}</div>
            <h2>Code:</h2>
            <pre><code>{html.escape(code)}</code></pre>
            <h2>Output:</h2>
            <pre>{html.escape(''.join(terminal_log))}</pre>
        </body>
        </html>
        """
        
        with open("output.html", "w") as f:
            f.write(html_content)
            
        pdf_filename = f"{re.sub(r'[^a-zA-Z0-9]', '_', title)}.pdf"
        subprocess.run(["wkhtmltopdf", "output.html", pdf_filename], check=True)
        
        with open(pdf_filename, "rb") as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=pdf_filename,
                caption=f"Execution report for {title}"
            )
            
    except Exception as e:
        logger.error(f"PDF generation error: {str(e)}")
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)

async def cleanup(context: CallbackContext):
    try:
        process = context.user_data.get('process')
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                
        for filename in ["temp.c", "temp", "output.html"]:
            if os.path.exists(filename):
                os.remove(filename)
                
        for file in os.listdir():
            if file.endswith(".pdf"):
                os.remove(file)
                
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Cleanup error: {str(e)}")

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
        
        logger.info("Starting bot...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except telegram.error.Conflict:
        logger.error("Another instance is already running. Exiting.")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        raise

if __name__ == '__main__':
    main()
