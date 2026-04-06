#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""UCP Merchant Server (Python/FastAPI)."""

import logging
import sys
from collections.abc import Sequence
from absl import app as absl_app
import config
from exceptions import UcpError
from fastapi import FastAPI
from fastapi import Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import csv
from pathlib import Path
from mcp_wrapper import mcp_dispatcher
import generated_routes.ucp_routes
from routes.discovery import router as discovery_router
from routes.order import router as order_router
import routes.ucp_implementation
import dependencies
import uvicorn

# --- App Setup ---

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
  title="UCP Shopping Service",
  version=config.get_server_version(),
  description="Reference implementation of the UCP Shopping Service",
  lifespan=config.lifespan,
)


@app.exception_handler(UcpError)
async def ucp_exception_handler(request: Request, exc: UcpError):
  """Handle UCP-specific exceptions and converts them to JSON responses."""
  del request  # Unused.
  return JSONResponse(
    status_code=exc.status_code,
    content={"detail": exc.message, "code": exc.code},
  )


# Apply business logic implementation to generated routes
routes.ucp_implementation.apply_implementation(
  generated_routes.ucp_routes.router
)
app.include_router(
  generated_routes.ucp_routes.router, 
  dependencies=[Depends(dependencies.verify_access_token)],
)
app.include_router(order_router, 
  dependencies=[Depends(dependencies.verify_access_token)],
)
app.include_router(discovery_router)

images_dir = Path(__file__).resolve().parent / "images"
images_dir.mkdir(exist_ok=True)
app.mount("/images", StaticFiles(directory=str(images_dir)), name="images")

@app.get("/page", response_class=HTMLResponse)
async def botanic_page():
  """Simple static frontend for botanic.com"""
  products_path = Path(config.FLAGS.static_data_dir) / "products.csv"
  inventory_path = Path(config.FLAGS.static_data_dir) / "inventory.csv"
  
  products = {}
  try:
    with open(products_path, newline="", encoding="utf-8") as f:
      reader = csv.DictReader(f)
      for row in reader:
        products[row["id"]] = row
  except Exception as e:
    logger.error(f"Error reading products: {e}")
      
  try:
    with open(inventory_path, newline="", encoding="utf-8") as f:
      reader = csv.DictReader(f)
      for row in reader:
        if row["product_id"] in products:
          products[row["product_id"]]["quantity"] = row["quantity"]
  except Exception as e:
    logger.error(f"Error reading inventory: {e}")

  template_path = Path(__file__).resolve().parent / "frontend" / "page.html"
  try:
    html_template = template_path.read_text(encoding="utf-8")
  except Exception as e:
    logger.error(f"Error reading template: {e}")
    html_template = "<html><body><h1>Template missing</h1><!-- PRODUCTS_PLACEHOLDER --></body></html>"

  products_html = ""
  for _, p in products.items():
    qty = int(p.get("quantity", 0))
    if qty > 0:
      stock_html = f'<div class="stock-status in-stock">Available ({qty})</div>'
    else:
      stock_html = f'<div class="stock-status out-of-stock">Sold Out</div>'
      
    price_val = float(p.get("price", 0))
    price_str = f"${price_val/100:.2f}"
    
    # Use local image path matching the filename in the CSV URL
    img_url = p.get("image_url", "")
    image_filename = img_url.split("/")[-1] if "/" in img_url else img_url
    local_img_path = f"/images/{image_filename}"
    
    products_html += f"""
        <div class="product-card">
            <div class="image-wrapper">
                <img src="{local_img_path}" alt="{p.get('title', '')}">
            </div>
            <div class="product-info">
                <h3 class="product-title">{p.get('title', '')}</h3>
                <div class="product-price">{price_str}</div>
                {stock_html}
            </div>
        </div>
    """
      
  final_html = html_template.replace("<!-- PRODUCTS_PLACEHOLDER -->", products_html)
  return final_html

# MCP JSON-RPC 2.0 Endpoint
app.add_api_route(
  "/ucp/mcp", mcp_dispatcher, methods=["POST"], 
  dependencies=[Depends(dependencies.verify_access_token)],
)



def main(argv: Sequence[str]) -> None:
  """Run the UCP Merchant Server."""
  del argv  # Unused.

  if (
    config.FLAGS.products_db_path is None
    or config.FLAGS.transactions_db_path is None
    or config.FLAGS.port is None
  ):
    logger.error(
      "Both --products_db_path, --transactions_db_path, and --port must be"
      " provided."
    )
    print("\nUsage:")  # noqa: T201
    print(config.FLAGS.main_module_help())  # noqa: T201
    sys.exit(1) 

  ssl_keyfile = config.FLAGS.ssl_keyfile
  ssl_certfile = config.FLAGS.ssl_certfile

  if ssl_keyfile and ssl_certfile:
    logger.info("Starting server with HTTPS enabled.")
    uvicorn.run(app, host="0.0.0.0", port=config.FLAGS.port, ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile)
  else:
    logger.info("Starting server with HTTP. For HTTPS, provide --ssl_keyfile and --ssl_certfile.")
    uvicorn.run(app, host="0.0.0.0", port=config.FLAGS.port)



if __name__ == "__main__":
  absl_app.run(main)
