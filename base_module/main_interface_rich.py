import os
import sys

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config_module.loader import config

console = Console()

# Set up key bindings for multi-line input
# Enter = submit, Ctrl+J = new line
bindings = KeyBindings()


@bindings.add("enter")
def handle_enter(event):
    """Enter submits the input."""
    event.current_buffer.validate_and_handle()


@bindings.add("c-j")  # Ctrl+J for newline
def handle_newline(event):
    """Ctrl+J inserts a newline."""
    event.current_buffer.insert_text("\n")


# Create prompt session with multi-line support
session = PromptSession(key_bindings=bindings, multiline=True)

# Point to your running ArkOS agent
client = OpenAI(base_url=f"http://localhost:{config.get('app.port')}/v1", api_key="not-needed")

# Conversation history for display
conversation_history = []


def display_header():
    """Display the welcome header."""
    header = Table.grid(padding=1)
    header.add_column(justify="center")
    header.add_row("[bold cyan]ArkOS[/bold cyan]")
    header.add_row("[dim]Intelligent Agent Interface[/dim]")

    console.print(Panel(header, border_style="cyan", padding=(1, 2)))
    console.print("[dim]Type [bold]/help[/bold] for commands, [bold]/exit[/bold] to quit[/dim]\n")


def display_message(role: str, content: str):
    """Display a message in a styled panel."""
    if role == "user":
        console.print(
            Panel(
                content,
                title="[bold blue]You[/bold blue]",
                title_align="left",
                border_style="blue",
                padding=(0, 1),
            )
        )
    else:
        # Render assistant responses as markdown
        console.print(
            Panel(
                Markdown(content),
                title="[bold green]ARK[/bold green]",
                title_align="left",
                border_style="green",
                padding=(0, 1),
            )
        )


def chat_stream(prompt: str) -> str:
    """Send a message and stream the response with rich UI."""
    conversation_history.append({"role": "user", "content": prompt})
    display_message("user", prompt)

    full_response = ""

    with Live(
        Panel(
            Spinner("dots", text="Thinking..."),
            border_style="green",
            title="[bold green]ARK[/bold green]",
            title_align="left",
        ),
        console=console,
        refresh_per_second=10,
        transient=True,
    ) as live:
        stream = client.chat.completions.create(
            model="ark-agent",
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )

        for chunk in stream:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_response += content

                # Update live display with accumulated response
                live.update(
                    Panel(
                        Markdown(full_response + "█"),
                        title="[bold green]ARK[/bold green]",
                        title_align="left",
                        border_style="green",
                        padding=(0, 1),
                    )
                )

    # Final display without cursor
    if full_response:
        console.print(
            Panel(
                Markdown(full_response),
                title="[bold green]ARK[/bold green]",
                title_align="left",
                border_style="green",
                padding=(0, 1),
            )
        )
        conversation_history.append({"role": "assistant", "content": full_response})
    else:
        console.print(
            Panel(
                "[dim italic]No response received[/dim italic]",
                title="[bold green]ARK[/bold green]",
                title_align="left",
                border_style="yellow",
                padding=(0, 1),
            )
        )

    return full_response


def chat(prompt: str) -> str:
    """Send a message and get full response (no streaming) with rich UI."""
    conversation_history.append({"role": "user", "content": prompt})
    display_message("user", prompt)

    with console.status("[bold green]Thinking...", spinner="dots"):
        response = client.chat.completions.create(
            model="ark-agent",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )

    message = response.choices[0].message.content
    display_message("assistant", message)
    conversation_history.append({"role": "assistant", "content": message})

    return message


def show_help():
    """Display help information."""
    help_text = """
[bold]Commands:[/bold]
  [cyan]/help[/cyan]      Show this help message
  [cyan]/clear[/cyan]     Clear the screen
  [cyan]/history[/cyan]   Show conversation history
  [cyan]/stream[/cyan]    Toggle streaming mode
  [cyan]/exit[/cyan]      Exit the application

[bold]Input:[/bold]
  [cyan]Enter[/cyan]    Submit message
  [cyan]Ctrl+J[/cyan]   New line

[bold]Tips:[/bold]
  - Press Ctrl+C to cancel current operation
  - Responses are rendered as Markdown
"""
    console.print(Panel(help_text, title="[bold]Help[/bold]", border_style="cyan"))


def show_history():
    """Display conversation history."""
    if not conversation_history:
        console.print("[dim]No conversation history yet.[/dim]")
        return

    console.print(Panel("[bold]Conversation History[/bold]", border_style="cyan"))
    for msg in conversation_history:
        display_message(msg["role"], msg["content"])


def main():
    """Main CLI loop."""
    console.clear()
    display_header()

    use_streaming = True

    while True:
        try:
            console.print()
            user_input = session.prompt("You: ")

            if not user_input.strip():
                continue

            cmd = user_input.strip().lower()

            # Handle commands
            if cmd in ["/exit", "/quit", "exit", "quit"]:
                console.print("\n[dim]Goodbye![/dim]")
                break
            elif cmd == "/help":
                show_help()
                continue
            elif cmd == "/clear":
                console.clear()
                display_header()
                continue
            elif cmd == "/history":
                show_history()
                continue
            elif cmd == "/stream":
                use_streaming = not use_streaming
                mode = "enabled" if use_streaming else "disabled"
                console.print(f"[dim]Streaming {mode}[/dim]")
                continue

            # Send message to agent
            console.print()
            if use_streaming:
                chat_stream(user_input)
            else:
                chat(user_input)

        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted. Type /exit to quit.[/dim]")
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break
        except Exception as e:
            console.print(
                Panel(
                    f"[red]{str(e)}[/red]",
                    title="[bold red]Error[/bold red]",
                    border_style="red",
                )
            )


if __name__ == "__main__":
    main()
