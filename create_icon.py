#!/usr/bin/env python3
"""Generate dinosaur icon from the provided image."""

import os
from PIL import Image, ImageDraw

def create_dinosaur_icon(output_dir: str = "dist"):
    """
    Create a dinosaur-themed icon with better shape definition.
    Generates 256x256 PNG and ICO versions.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Create a new image with transparent background
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw dinosaur (T-Rex style with better proportions)
    # Main body (large ellipse)
    body_left, body_top, body_right, body_bottom = 45, 110, 180, 180
    draw.ellipse([body_left, body_top, body_right, body_bottom], 
                 fill=(220, 60, 50), outline=(160, 30, 25), width=3)
    
    # Neck connector (curved triangle approximation)
    draw.polygon([(175, 120), (195, 95), (185, 135)], 
                 fill=(220, 60, 50), outline=(160, 30, 25))
    
    # Head (large circle)
    draw.ellipse([170, 60, 230, 120], 
                 fill=(220, 60, 50), outline=(160, 30, 25), width=3)
    
    # Snout/jaw (extended polygon)
    draw.polygon([(230, 85), (250, 80), (255, 90), (240, 100)], 
                 fill=(200, 40, 35), outline=(160, 30, 25))
    
    # Eye (white circle with pupil)
    draw.ellipse([200, 75, 220, 95], 
                 fill=(255, 255, 255), outline=(0, 0, 0), width=2)
    draw.ellipse([208, 83, 218, 93], 
                 fill=(0, 0, 0))
    
    # Nostril
    draw.ellipse([240, 88, 248, 96], fill=(0, 0, 0))
    
    # Back spikes/plates (green)
    spike_color = (76, 175, 80)
    spike_outline = (40, 120, 50)
    
    spike_positions = [
        (70, 95),   # front
        (95, 80),   # mid-front
        (120, 75),  # mid
        (145, 85),  # mid-back
    ]
    
    for x, y in spike_positions:
        # Draw triangular spikes
        draw.polygon([(x, y), (x-10, y-22), (x+10, y-22)], 
                    fill=spike_color, outline=spike_outline)
    
    # Tail (long curved shape using polygon)
    tail_points = [
        (165, 165),   # base
        (190, 180),   # curve down-right
        (210, 175),   # curve back
        (230, 190),   # extend far
        (220, 200),   # tip curve
        (195, 185),   # back in
        (175, 175),   # back to body
    ]
    draw.polygon(tail_points, fill=(200, 40, 35), outline=(160, 30, 25))
    
    # Legs (4 pillars)
    leg_color = (190, 50, 45)
    leg_outline = (140, 20, 15)
    
    leg_positions = [
        (60, 175, 75, 240),    # front-left
        (100, 175, 115, 240),  # front-right
        (130, 175, 145, 240),  # back-left
        (165, 175, 180, 240),  # back-right
    ]
    
    for x1, y1, x2, y2 in leg_positions:
        draw.rectangle([x1, y1, x2, y2], fill=leg_color, outline=leg_outline, width=2)
    
    # Claws on feet
    claw_color = (100, 20, 15)
    for x1, y1, x2, y2 in leg_positions:
        # Add claw points at bottom
        mid_x = (x1 + x2) / 2
        draw.polygon([(mid_x - 4, y2), (mid_x, y2 + 8), (mid_x + 4, y2)], 
                    fill=claw_color)
    
    # Save as PNG
    png_path = os.path.join(output_dir, "app_icon.png")
    img.save(png_path, "PNG")
    print(f"✓ Saved PNG icon: {png_path}")

    # Convert to ICO (multiple sizes for better quality)
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico_images = []
    for ico_size in ico_sizes:
        ico_images.append(img.resize(ico_size, Image.Resampling.LANCZOS))

    ico_path = os.path.join(output_dir, "app_icon.ico")
    # Save with all sizes embedded in one ICO file
    ico_images[0].save(ico_path, "ICO", append_images=ico_images[1:])
    print(f"✓ Saved ICO icon: {ico_path}")

if __name__ == "__main__":
    create_dinosaur_icon()
