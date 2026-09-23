import sys
import io
import os
from PIL import Image
from dotenv import load_dotenv
load_dotenv()
from image_generator import ImageGenerator

def main():
    gen = ImageGenerator()
    product_info = {
        'product_name': '메종드꼼마 스칸딕 체어 식탁의자',
        'image_urls': ['https://thumbnail9.coupangcdn.com/thumbnails/remote/600x600ex/image/retail/images/2023/11/14/14/9/f822ac4b-6f8d-4fe3-94c6-2c1b2ecda133.jpg']
    }
    img = gen._download_image(product_info['image_urls'][0])
    print(f"Original image size: {img.size}")
    
    # agy 게이트웨이 (AGY_GATEWAY_URL/AGY_GATEWAY_TOKEN)
    prompt = "Create a lifestyle photo of this chair in a dining room."
    print("Requesting from agy gateway...")
    res_img = gen._generate_with_gemini(img, prompt)
    if res_img:
        print(f"Generated image size: {res_img.size}")
    else:
        print("Failed to generate image.")
        
if __name__ == '__main__':
    main()
