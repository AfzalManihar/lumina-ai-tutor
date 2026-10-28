"""Safe configuration diagnostic; never prints secret values."""

import os
from dotenv import load_dotenv


if __name__ == "__main__":
    load_dotenv()
    print(f"MONGO_URI configured: {'yes' if os.getenv('MONGO_URI') else 'no'}")
    print(f"GROQ_API_KEY configured: {'yes' if os.getenv('GROQ_API_KEY') else 'no'}")
