import os.path
import sys
import base64
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def extract_from_address(headers):
    """Extract 'From' address from email headers."""
    from_address = None
    for header in headers:
        if header['name'].lower() == 'from':
            from_address = header['value']
    return from_address

def main():
    """Shows basic usage of the Gmail API.
    Lists the user's Gmail labels.
    """
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    try:
        # Call the Gmail API
        service = build('gmail', 'v1', credentials=creds)

        # Step 1: Read the email sender from command line
        email_sender = sys.argv[1]

        # Step 2: get the most recent email threads
        threads = service.users().threads().list(userId='me').execute().get('threads', [])

        email_sender = sys.argv[1]
        write_to = sys.argv[2]

        FINISHED = False
        # Step 2a: Fetch the most recent thread with messages from the sender
        for thread in threads:
            tdata = service.users().threads().get(userId='me', id=thread['id']).execute()
            nmsgs = len(tdata['messages'])
            user_id = 'me'

            if FINISHED:
                break

            if nmsgs > 0:   # skip if <1 msgs in thread
                for message in tdata['messages']:
                    from_address = extract_from_address(message['payload']['headers'])
                    
                    if email_sender in from_address:
                        print(from_address)
                        parts = [message['payload']]
                        while parts:
                            part = parts.pop()
                            if part.get('parts'):
                                parts.extend(part['parts'])
                            if part.get('filename'):
                                if 'data' in part['body']:
                                    file_data = base64.urlsafe_b64decode(part['body']['data'].encode('UTF-8'))
                                    print('FileData for %s, %s found! size: %s' % (message['id'], part['filename'], part['size']))
                                elif 'attachmentId' in part['body']:
                                    attachment = service.users().messages().attachments().get(
                                        userId=user_id, messageId=message['id'], id=part['body']['attachmentId']
                                    ).execute()
                                    file_data = base64.urlsafe_b64decode(attachment['data'].encode('UTF-8'))
                                    print('FileData for %s, %s found! size: %s' % (message['id'], part['filename'], attachment['size']))
                                else:
                                    file_data = None
                                if file_data:
                                    path = ''.join([write_to, part['filename']])
                                    with open(path, 'wb') as f:
                                        f.write(file_data)  
                                    
                                    FINISHED = True
    except HttpError as error:
        print(f'An HTTP error occurred: {error}')

if __name__ == '__main__':
    main()