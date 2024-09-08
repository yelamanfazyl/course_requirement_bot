# Use the AWS Lambda Python 3.9 base image
FROM amazon/aws-lambda-python:3.9

# Copy your Python requirements file and install dependencies
COPY requirements.txt ./
RUN pip install -r requirements.txt -t .

# Copy the rest of your application code
COPY . .

# Set the CMD to your Lambda function handler
CMD ["lambda_function.lambda_handler"]
