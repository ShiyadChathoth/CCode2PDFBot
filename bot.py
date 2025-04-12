async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")

        # Generate HTML with proper tab alignment styling and page break control
        html_content = f"""
        <html>
        <head>
            <style>
                @page {{
                    size: A4;
                    margin: 20mm;
                }}
                body {{
                    font-family: Arial, sans-serif;
                    margin: 0;
                    padding: 0;
                }}
                .content-container {{
                    display: flex;
                    flex-direction: column;
                }}
                .program-title {{
                    font-size: 30px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 20px;
                    text-decoration: underline;
                    text-decoration-thickness: 5px;
                    border-bottom: 3px;
                }}
                pre {{
                    font-family: 'Courier New', monospace;
                    white-space: pre;
                    font-size: 18px;
                    line-height: 1.3;
                    tab-size: 8;
                    -moz-tab-size: 8;
                    -o-tab-size: 8;
                    background: #FFFFFF;
                    padding: 5px;
                    border-radius: 3px;
                    overflow-x: auto;
                }}
                .code-section {{
                    margin-bottom: 20px;
                }}
                .terminal-view {{
                    margin: 10px 0;
                }}
                .output-title {{
                    font-size: 25px;
                    text-decoration: underline;
                    text-decoration-thickness: 5px;
                    font-weight: bold;
                    margin-top: 20px;
                    page-break-before: auto;
                    page-break-after: avoid;
                }}
                .output-content {{
                    page-break-before: avoid;
                }}
            </style>
        </head>
        <body>
            <div class="content-container">
                <div class="program-title">{html.escape(program_title)}</div>
                <div class="code-section">
                    <pre><code>{html.escape(code)}</code></pre>
                </div>
                <div class="terminal-view">
                    {reconstruct_terminal_view(context)}
                </div>
            </div>
        </body>
        </html>
        """

        with open("output.html", "w") as file:
            file.write(html_content)

        # Generate sanitized filename from title
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)
        pdf_filename = f"{sanitized_title}.pdf"
        
        # Use specific wkhtmltopdf options to control page fill
        subprocess.run([
            "wkhtmltopdf",
            "--enable-smart-shrinking",
            "--disable-smart-page-breaks",  # Disable smart page breaks
            "--print-media-type",
            "--page-size", "A4",
            "--margin-top", "20mm",
            "--margin-bottom", "20mm",
            "--margin-left", "20mm",
            "--margin-right", "20mm",
            "--minimum-font-size", "12",
            "output.html", 
            pdf_filename
        ])

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
        <div class="output-section">
            <h1 class="output-title">OUTPUT</h1>
            <div class="output-content" style="
                font-family: 'Courier New', monospace;
                white-space: pre;
                font-size: 18px;
                line-height: 1.2;
                background: #FFFFFF;
                padding: 10px;
                border-radius: 3px;
                overflow-x: auto;
            ">{html.escape(raw_output)}</div>
        </div>
        """
    
    return "<pre>No terminal output available</pre>"
