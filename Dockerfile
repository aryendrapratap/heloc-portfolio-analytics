FROM python:3.14-slim

WORKDIR /app

# Install packages first, so this step is reused when only the code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

# Build the database, load the sample data, then start the dashboard
CMD ["sh", "-c", "python scripts/build_db.py && python data_generator/generate_data.py && streamlit run dashboard/app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true"]
