# Use a specific and recent Python slim image for reproducibility and efficiency.
FROM python:3.10.13-slim

# The working directory is now set to the root of the container.
WORKDIR /

# Copy the requirements file first to optimize Docker's build cache.
COPY requirements.txt .

# Install dependencies from requirements.txt and also install openai-agents directly.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir openai-agents

# Copy the rest of your files directly into the container's root.
COPY . .

# Create and set permissions for necessary files/directories.
RUN mkdir -p .files .chainlit && \
    touch chainlit.md

# Create a non-root user for security.
RUN adduser --disabled-password --gecos "" chainlit
RUN chown -R chainlit:chainlit .files .chainlit chainlit.md
USER chainlit

# Set Chainlit environment variables.
ENV CHAINLIT_UI=True
ENV CHAINLIT_BROWSER_AUTO_OPEN=false

# Expose the port on which the application will run.
EXPOSE 7860

# Command to run the application.
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "7860"]