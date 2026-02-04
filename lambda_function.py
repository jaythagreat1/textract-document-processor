import json
import boto3
import os
from datetime import datetime

# Initialize AWS clients
textract_client = boto3.client('textract', region_name='us-west-2')
s3_client = boto3.client('s3', region_name='us-west-2')

def lambda_handler(event, context):
    """
    Processes documents using AWS Textract
    Handles both direct invocation and API Gateway events
    """
    try:
        # Handle API Gateway event format
        if 'body' in event:
            if isinstance(event['body'], str):
                body = json.loads(event['body'])
            else:
                body = event['body']
            bucket = body.get('bucket') or os.environ.get('DOCUMENT_BUCKET')
            key = body.get('key')
        else:
            # Direct invocation
            bucket = event.get('bucket') or os.environ.get('DOCUMENT_BUCKET')
            key = event.get('key')
        
        if not bucket or not key:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Missing bucket or key parameter'})
            }
        
        print(f"Processing document: s3://{bucket}/{key}")
        
        # Call Textract
        print("Calling Textract DetectDocumentText...")
        text_response = textract_client.detect_document_text(
            Document={
                'S3Object': {
                    'Bucket': bucket,
                    'Name': key
                }
            }
        )
        
        # Extract text
        extracted_text = extract_text(text_response)
        
        print("Calling Textract AnalyzeDocument...")
        analyze_response = textract_client.analyze_document(
            Document={
                'S3Object': {
                    'Bucket': bucket,
                    'Name': key
                }
            },
            FeatureTypes=['FORMS', 'TABLES']
        )
        
        # Extract forms and tables
        forms = extract_forms(analyze_response)
        tables = extract_tables(analyze_response)
        
        # Create result
        result = {
            'document_info': {
                'source': f"s3://{bucket}/{key}",
                'file_type': key.split('.')[-1],
                'processed_at': datetime.utcnow().isoformat(),
                'page_count': len([b for b in text_response['Blocks'] if b['BlockType'] == 'PAGE'])
            },
            'extracted_text': extracted_text,
            'forms': forms,
            'tables': tables,
            'statistics': {
                'total_blocks': len(text_response['Blocks']),
                'words': len([b for b in text_response['Blocks'] if b['BlockType'] == 'WORD']),
                'lines': len([b for b in text_response['Blocks'] if b['BlockType'] == 'LINE']),
                'forms_found': len(forms),
                'tables_found': len(tables)
            }
        }
        
        # Save to S3
        result_key = f"results/{key.split('/')[-1]}.json"
        s3_client.put_object(
            Bucket=bucket,
            Key=result_key,
            Body=json.dumps(result, indent=2),
            ContentType='application/json'
        )
        
        print(f"Results saved to: s3://{bucket}/{result_key}")
        
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'Document processing completed',
                'result_location': f"s3://{bucket}/{result_key}",
                'statistics': result['statistics'],
                'preview': extracted_text[:500] + '...' if len(extracted_text) > 500 else extracted_text
            }, indent=2)
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'message': 'Failed to process document'
            })
        }

def extract_text(response):
    """Extract all text from Textract response"""
    text_blocks = []
    for block in response['Blocks']:
        if block['BlockType'] == 'LINE':
            text_blocks.append(block['Text'])
    return '\n'.join(text_blocks)

def extract_forms(response):
    """Extract key-value pairs from forms"""
    forms = {}
    key_map = {}
    value_map = {}
    block_map = {}
    
    for block in response['Blocks']:
        block_id = block['Id']
        block_map[block_id] = block
        
        if block['BlockType'] == 'KEY_VALUE_SET':
            if 'KEY' in block['EntityTypes']:
                key_map[block_id] = block
            else:
                value_map[block_id] = block
    
    for key_block_id, key_block in key_map.items():
        value_block = find_value_block(key_block, value_map)
        key_text = get_text(key_block, block_map)
        value_text = get_text(value_block, block_map) if value_block else ''
        
        if key_text and value_text:
            forms[key_text] = value_text
    
    return forms

def extract_tables(response):
    """Extract tables from document"""
    tables = []
    table_blocks = [block for block in response['Blocks'] if block['BlockType'] == 'TABLE']
    
    for table_block in table_blocks:
        table = []
        if 'Relationships' in table_block:
            for relationship in table_block['Relationships']:
                if relationship['Type'] == 'CHILD':
                    cell_ids = relationship['Ids']
                    cells = [block for block in response['Blocks'] if block['Id'] in cell_ids]
                    
                    rows = {}
                    for cell in cells:
                        if cell['BlockType'] == 'CELL':
                            row_index = cell.get('RowIndex', 1)
                            col_index = cell.get('ColumnIndex', 1)
                            
                            if row_index not in rows:
                                rows[row_index] = {}
                            
                            cell_text = ''
                            if 'Relationships' in cell:
                                for rel in cell['Relationships']:
                                    if rel['Type'] == 'CHILD':
                                        for word_id in rel['Ids']:
                                            word = next((b for b in response['Blocks'] if b['Id'] == word_id), None)
                                            if word and word['BlockType'] == 'WORD':
                                                cell_text += word['Text'] + ' '
                            
                            rows[row_index][col_index] = cell_text.strip()
                    
                    for row_idx in sorted(rows.keys()):
                        row = rows[row_idx]
                        table.append([row.get(col, '') for col in sorted(row.keys())])
        
        if table:
            tables.append(table)
    
    return tables

def find_value_block(key_block, value_map):
    """Find the value block associated with a key block"""
    if 'Relationships' in key_block:
        for relationship in key_block['Relationships']:
            if relationship['Type'] == 'VALUE':
                for value_id in relationship['Ids']:
                    if value_id in value_map:
                        return value_map[value_id]
    return None

def get_text(block, block_map):
    """Get text from a block"""
    text = ''
    if 'Relationships' in block:
        for relationship in block['Relationships']:
            if relationship['Type'] == 'CHILD':
                for child_id in relationship['Ids']:
                    if child_id in block_map:
                        word = block_map[child_id]
                        if word['BlockType'] == 'WORD':
                            text += word['Text'] + ' '
    return text.strip()
