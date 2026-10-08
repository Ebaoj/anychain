"""Error texts without endpoint addresses (D46, D47): an RPC URL can carry the provider's key, and errors also
name a host alone ("outage of rpc.example.com"). Used for what the reader sees and for what the model is told."""
import re

URL = re.compile(r"(?:https?|wss?)://\S+")


def no_urls(text: str, hidden_hosts: tuple[str, ...] = ()) -> str:
    text = URL.sub("<endpoint>", text)
    for host in hidden_hosts:
        if host:
            text = text.replace(host, "<node>")
    return text
