from django.shortcuts import render, redirect
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import JsonResponse
from django.contrib import messages
from supabase_client import supabase, supabase_service
from supabase_client import supabase_service
from .decorators import admin_required
from django.views.decorators.http import require_POST
from .utils import log_activity, get_greeting
from .decorators import admin_required, student_required
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from .decorators import student_required
import json
import pytz
import uuid
import math
import requests

def dashboard_redirect(request):
    """
    Redirects an authenticated user to their appropriate dashboard (admin or student).
    
    Checks if the user is authenticated and retrieves their user_type to determine
    which dashboard to display. Unauthenticated users are redirected to the login page.
    Returns a redirect response to either the admin or student dashboard.
    """
    if not hasattr(request.user, 'is_authenticated') or not request.user.is_authenticated:
        messages.error(request, 'Please login to access the dashboard.')
        return redirect('login')
    
    user_type = getattr(request.user, 'user_type', 'student')
    if user_type == 'admin':
        return redirect('admin_dashboard')
    return redirect('student_dashboard')



# --- Student Views ---

@student_required
@require_http_methods(["POST"]) # Only allow POST requests
def mark_notifications_as_read(request):
    """
    Marks all unread notifications for the current user as 'read'.
    
    Handles AJAX POST requests to update the user's unread notifications to read status.
    Uses row-level security (RLS) to ensure users can only modify their own notifications.
    Returns JSON response indicating success or failure.
    """
    # We only want AJAX requests
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)

    try:
        # Use the user's RLS-enabled client (supabase).
        # This will ONLY update their own notifications.
        supabase.table('notifications') \
            .update({'is_read': True}) \
            .eq('user_id', request.user.id) \
            .eq('is_read', False) \
            .execute()

        # Return success
        return JsonResponse({'success': True})

    except Exception as e:
        print(f"Error marking notifications as read: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
    
@student_required
@require_http_methods(["POST"])
def mark_all_as_read_header_view(request):
    """
    Marks all unread notifications for a user as 'read' via AJAX request.
    
    Processes an AJAX POST request to update all pending unread notifications
    to read status. Returns the new unread count (0) for updating the UI notification badge.
    """
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    
    try:
        supabase.table('notifications') \
            .update({'is_read': True}) \
            .eq('user_id', request.user.id) \
            .eq('is_read', False) \
            .execute()
        
        # After updating, the new unread count is 0
        return JsonResponse({'success': True, 'new_unread_count': 0})

    except Exception as e:
        print(f"Error marking all notifications as read: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@student_required
@require_http_methods(["POST"])
def mark_notification_read_and_redirect(request, notification_id):
    """
    Marks a single notification as read and redirects user to the notification's link.
    
    Retrieves a specific notification's URL, marks it as read, and returns a redirect URL
    for the frontend to navigate to. Provides a fallback URL if the notification link is missing.
    Returns JSON response with redirect URL or error message.
    """
    # Default URL if something goes wrong
    fallback_url = '/dashboard/student/my-orders/' 
    
    try:
        response = supabase.table('notifications') \
            .select('link_url') \
            .eq('id', notification_id) \
            .eq('user_id', request.user.id) \
            .single() \
            .execute()

        if response.data and response.data.get('link_url'):
            link_url = response.data['link_url']
        else:
            link_url = fallback_url

        # After getting the link, mark the notification as read
        supabase.table('notifications') \
            .update({'is_read': True}) \
            .eq('id', notification_id) \
            .eq('user_id', request.user.id) \
            .execute()

        return JsonResponse({'success': True, 'redirect_url': link_url})

    except Exception as e:
        print(f"Error marking single notification as read: {e}")
        link_url = fallback_url # Send to default page on error
    
    return redirect(link_url)

@student_required
def all_notifications_view(request):
    """
    Displays all notifications (both read and unread) for the authenticated student.
    
    Renders a comprehensive notifications page by fetching detailed notification data
    including product information using an RPC function. Handles date parsing and provides
    user-friendly error messages if data retrieval fails.
    """
    all_notifications = []
    try:
        user_id = request.user.id
        # Call the new RPC function we just created
        response = supabase_service.rpc('get_my_detailed_notifications', {'p_user_id': user_id}).execute()
        
        if response.data:
            for item in response.data:
                # Parse the date string into a datetime object
                try:
                    item['created_at'] = datetime.fromisoformat(item['created_at'])
                except (ValueError, TypeError):
                    item['created_at'] = None
                
                all_notifications.append(item)
                
    except Exception as e:
        messages.error(request, f"Could not fetch your notifications: {e}")
        
    context = {
        'all_notifications': all_notifications,
        'active_page': 'notifications', # For highlighting the nav link
        'page_title': 'All Notifications',
    }
    return render(request, 'dashboards/all_notifications.html', context)

@student_required
@require_http_methods(["POST"])
def batch_update_notifications(request):
    """
    Handles batch updates to notification read status via AJAX.
    
    Processes multiple notification IDs to mark them as read or unread in a single operation.
    Validates input, updates the database, and returns the new total unread count for UI updates.
    Returns JSON response with success status and updated notification count.
    """
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    
    try:
        notification_ids_str = request.POST.get('notification_ids')
        action = request.POST.get('action') # "mark_read" or "mark_unread"

        if not notification_ids_str or not action:
            raise ValueError("Missing 'notification_ids' or 'action'.")

        notification_ids = [int(nid) for nid in notification_ids_str.split(',') if nid.isdigit()]
        
        if not notification_ids:
            raise ValueError("No valid notification IDs provided.")

        new_status = True if action == 'mark_read' else False
        
        supabase.table('notifications') \
            .update({'is_read': new_status}) \
            .in_('id', notification_ids) \
            .eq('user_id', request.user.id) \
            .execute()

        # After updating, get the new total unread count (using RLS-enabled client)
        count_response = supabase.table('notifications') \
            .select('id', count='exact') \
            .eq('is_read', False) \
            .execute()
        
        return JsonResponse({
            'success': True, 
            'message': f'{len(notification_ids)} notifications updated.',
            'new_unread_count': count_response.count  
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@student_required
@require_http_methods(["POST"])
def batch_delete_notifications(request):
    """
    Permanently deletes a batch of notifications via AJAX request.
    
    Processes multiple notification IDs for deletion while ensuring the user
    only deletes their own notifications. Returns the updated unread count after deletion.
    Returns JSON response with success status and new notification count.
    """
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    
    try:
        notification_ids_str = request.POST.get('notification_ids')
        if not notification_ids_str:
            raise ValueError("Missing 'notification_ids'.")

        notification_ids = [int(nid) for nid in notification_ids_str.split(',') if nid.isdigit()]
        
        if not notification_ids:
            raise ValueError("No valid notification IDs provided.")
        
        supabase_service.table('notifications') \
            .delete() \
            .in_('id', notification_ids) \
            .eq('user_id', request.user.id) \
            .execute()
        
        # After deleting, get the new total unread count (using RLS-enabled client)
        count_response = supabase.table('notifications') \
            .select('id', count='exact') \
            .eq('is_read', False) \
            .execute()
        
        return JsonResponse({
            'success': True, 
            'message': f'{len(notification_ids)} notifications deleted.',
            'new_unread_count': count_response.count  
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@student_required
@require_http_methods(["POST"])
def mark_all_as_read_view(request):
    """
    Marks ALL unread notifications as read via AJAX request.
    
    Updates all pending unread notifications for the authenticated user to read status
    in a single operation. Returns success confirmation with new unread count (0).
    Returns JSON response indicating completion.
    """
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    
    try:
        supabase.table('notifications') \
            .update({'is_read': True}) \
            .eq('user_id', request.user.id) \
            .eq('is_read', False) \
            .execute()
        
        # After marking all as read, the new unread count is 0.
        return JsonResponse({
            'success': True, 
            'message': 'All notifications marked as read.',
            'new_unread_count': 0  # We know the new count is 0
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@student_required
def student_dashboard(request):
    """
    Renders the main student dashboard with personalized welcome and order summaries.
    
    Displays a greeting message, user display name, and summaries of the most recent
    reservation and order. Fetches data via RPC functions and handles date parsing.
    Provides a welcoming interface with key information at a glance.
    """
    raw_name = getattr(request.user, 'get_full_name', lambda: 'Wildcat')()
    display_name = "Wildcat" # Default name

    # Parse the user's raw name (which could be a full name, email, or username)
    # to get a capitalized first name for the greeting.
    if isinstance(raw_name, str) and raw_name: 
        if ' ' in raw_name:
            # Full name like "Jairus Dave" -> "Jairus"
            display_name = raw_name.split(' ')[0]
        elif '@' in raw_name:
            # Email like "jairus.dave@cit.edu" -> "jairus"
            name_part = raw_name.split('@')[0] 
            if '.' in name_part:
                display_name = name_part.split('.')[0] 
            else:
                display_name = name_part 
        else:
            # Username like "jairus.dave" -> "jairus"
            if '.' in raw_name:
                display_name = raw_name.split('.')[0] 
            else:
                display_name = raw_name 

    display_name = display_name.capitalize()
    
    latest_reservation = None
    latest_order = None
    
    try:
        user_id = request.user.id
        
        # Fetch the most recent reservation
        res_response = supabase.rpc('get_my_detailed_reservations', {'p_user_id': user_id}).limit(1).execute()
        if res_response.data:
            latest_reservation = res_response.data[0]
            if latest_reservation.get('created_at'):
                latest_reservation['created_at'] = datetime.fromisoformat(latest_reservation['created_at'])
            if latest_reservation.get('expires_at'):
                latest_reservation['expires_at'] = datetime.fromisoformat(latest_reservation['expires_at'])

        # Fetch the most recent order
        order_response = supabase.rpc('get_my_orders', {'p_user_id': user_id}).limit(1).execute()
        if order_response.data:
            latest_order = order_response.data[0]
            if latest_order.get('created_at'):
                latest_order['created_at'] = datetime.fromisoformat(latest_order['created_at'])

            if latest_order.get('expires_at'):
                try:
                    latest_order['expires_at'] = datetime.fromisoformat(latest_order['expires_at'])
                except (ValueError, TypeError):
                    latest_order['expires_at'] = None

    except Exception as e:
        messages.error(request, "Could not load dashboard summaries.")

    context = {
        'display_name': display_name,
        'greeting': get_greeting(),
        'latest_reservation': latest_reservation,
        'latest_order': latest_order,
        'active_page': 'dashboard',
        'page_title': 'Dashboard',
    }
    return render(request, 'dashboards/student_dashboard.html', context)

@student_required
def browse_products_view(request):
    """
    Shows all products grouped by category with optional search filtering.

    Retrieves products from Supabase, applies search conditions if provided,
    organizes them by their category name, and returns the structured data
    to the template for rendering. Errors are caught and shown to the user.
    """
    
    search_query = request.GET.get('search', '').strip()
    categorized_products = defaultdict(list)

    try:
        # Base fetch query
        base_query = supabase.table('products') \
                             .select('*') \
                             .order('created_at', desc=True)

        # Apply search filter if needed
        if search_query:
            base_query = base_query.ilike('name', f'%{search_query}%')

        # Execute and collect products
        result = base_query.execute()
        products = result.data

        # Organize by category
        if products:
            for item in products:
                category = item.get('category') or 'Uncategorized'
                categorized_products[category].append(item)

    except Exception as error:
        messages.error(request, f"Could not fetch products: {error}")

    context = {
        'categorized_products': dict(categorized_products),
        'search_query': search_query,
        'active_page': 'browse',
        'page_title': 'Browse Products',
    }

    return render(request, 'dashboards/browse_products.html', context)

@student_required
def my_reservations_view(request):
    """
    Shows the logged-in student's pending reservations and backorders.

    Retrieves reservation data via RPC tied to the authenticated user. 
    Filters only items marked as 'pending', formats dates for readability, 
    and separates entries based on order_type for template rendering.
    """
    
    reservations = []
    backorders = []

    try:
        user_id = request.user.id
        rpc_result = supabase.rpc(
            'get_my_detailed_reservations',
            {'p_user_id': user_id}
        ).execute()

        records = rpc_result.data

        if records:
            for entry in records:

                # Skip anything not pending
                if entry.get('status') != 'pending':
                    continue

                # Parse datetime fields when present
                if entry.get('created_at'):
                    entry['created_at'] = datetime.fromisoformat(entry['created_at'])

                if entry.get('expires_at'):
                    entry['expires_at'] = datetime.fromisoformat(entry['expires_at'])

                # Categorize by type
                order_type = entry.get('order_type')
                if order_type == 'reservation':
                    reservations.append(entry)
                elif order_type == 'backorder':
                    backorders.append(entry)

    except Exception as error:
        messages.error(request, f"Could not fetch your reservations: {error}")

    context = {
        'reservations': reservations,
        'backorders': backorders,
        'active_page': 'reservations',
        'page_title': 'My Reservations',
    }

    return render(request, 'dashboards/my_reservations.html', context)

@student_required
def my_orders_view(request):
    """
    Displays student's complete order history organized by status categories.
    
    Fetches all orders and categorizes them into pending, approved, completed, and other
    (cancelled/rejected) statuses. Handles date and expiration time parsing for proper display.
    Provides comprehensive order tracking and history view.
    """

    pending_orders, approved_orders, completed_orders, other_orders = [], [], [], []

    try:
        user_id = request.user.id

        # Call the detailed function to get product and user info
        response = supabase.rpc(
            'get_my_detailed_orders',
            {'p_user_id': user_id}
        ).execute()

        if response.data:
            all_orders = response.data

            # Convert date strings to datetime objects for proper formatting
            for item in all_orders:

                # created_at parsing
                if item.get('created_at'):
                    created_at_str = item['created_at']
                    try:
                        item['created_at'] = datetime.fromisoformat(created_at_str)
                    except ValueError:
                        try:
                            item['created_at'] = datetime.strptime(
                                created_at_str, '%Y-%m-%dT%H:%M:%S.%f'
                            )
                        except ValueError:
                            item['created_at'] = None  # Or keep original string

                # expires_at parsing
                if item.get('expires_at'):
                    expires_at_str = item['expires_at']
                    try:
                        item['expires_at'] = datetime.fromisoformat(expires_at_str)
                    except ValueError:
                        try:
                            item['expires_at'] = datetime.strptime(
                                expires_at_str, '%Y-%m-%dT%H:%M:%S.%f'
                            )
                        except ValueError:
                            item['expires_at'] = None
                else:
                    item['expires_at'] = None  # Ensure the key exists

            # Separate orders into lists based on status
            for item in all_orders:
                status = item.get('status', 'unknown')

                if status == 'pending':
                    pending_orders.append(item)
                elif status == 'approved':
                    approved_orders.append(item)
                elif status == 'completed':
                    completed_orders.append(item)
                else:  # Includes cancelled, rejected, etc.
                    other_orders.append(item)

    except Exception as e:
        messages.error(request, f"Could not fetch your orders: {e}")
        pending_orders, approved_orders, completed_orders, other_orders = [], [], [], []

    context = {
        'pending_orders': pending_orders,
        'approved_orders': approved_orders,
        'completed_orders': completed_orders,
        'other_orders': other_orders,
        'search_query': '',  # JS handles filtering
        'active_page': 'orders',
        'page_title': 'My Orders',
    }

    return render(request, 'dashboards/my_orders.html', context)

@student_required
def batch_delete_orders_view(request):
    """ 
    Allows students to delete multiple completed or cancelled orders via AJAX.
    
    Processes batch deletion of "other" status orders (cancelled, rejected) that the
    student owns. Ensures data integrity by verifying ownership before deletion.
    Returns JSON confirmation with count of deleted orders.
    """
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        order_ids_str = request.POST.get('order_ids')
        try:
            order_ids = [int(oid) for oid in order_ids_str.split(',') if oid.isdigit()]
            if not order_ids:
                raise ValueError("No valid order IDs provided.")

            user_id = request.user.id

            # Use service role but ensure the user owns the orders
            supabase_service.table('orders') \
                .delete() \
                .in_('id', order_ids) \
                .eq('user_id', user_id) \
                .execute()

            return JsonResponse({'success': True, 'message': f"✅ {len(order_ids)} order(s) have been deleted."})

        except Exception as e:
            return JsonResponse({'success': False, 'error': f"An error occurred during batch deletion: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request.'}, status=400)

@student_required
def delete_single_order_view(request, order_id):
    """ 
    Allows students to delete a single completed or cancelled order via AJAX.
    
    Handles deletion of a specific "other" status order after verifying the student
    is the owner. Provides immediate feedback on successful deletion.
    Returns JSON response with status and confirmation message.
    """
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            user_id = request.user.id

            # Use service role but ensure the user owns the order
            supabase_service.table('orders') \
                .delete() \
                .eq('id', order_id) \
                .eq('user_id', user_id) \
                .execute()
            
            return JsonResponse({'success': True, 'message': f" Order #{order_id} has been permanently deleted."})

        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Failed to delete order #{order_id}: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request.'}, status=400)

@student_required
def create_reservation_view(request):
    """
    Processes AJAX POST request to create a new reservation or backorder.
    
    Calls Supabase RPC to reserve a product with specified quantity, deal method,
    and urgency status. Handles the response, fetches updated stock quantity,
    and returns product availability status for UI updates.
    Returns JSON with success status and new stock information.
    """
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            product_id = int(request.POST.get('product_id'))
            quantity_reserved = int(request.POST.get('quantity')) 

            params = {
                'p_product_id': product_id,
                'p_user_id': request.user.id,
                'p_quantity': quantity_reserved,
                'p_deal_method': request.POST.get('deal_method', 'meet-up'),
                'p_is_urgent': 'is_urgent' in request.POST
            }

            response = supabase.rpc('create_reservation', params).execute()

            if hasattr(response, 'error') and response.error:
                    raise Exception(str(response.error))
            if isinstance(response.data, list) and len(response.data) > 0 and 'error' in response.data[0]:
                    raise Exception(response.data[0]['error'])

            # After success, fetch the product's current stock quantity
            new_stock_quantity = None
            stock_response = supabase.table('products').select('stock_quantity').eq('id', product_id).single().execute()
            if stock_response.data:
                new_stock_quantity = stock_response.data.get('stock_quantity')

            if new_stock_quantity is None:
                new_stock_quantity = 0 # Default if fetch failed

            success_message = '✅ Your reservation has been placed successfully!'

            return JsonResponse({
                'success': True,
                'message': success_message,
                'product_id': product_id,
                'new_stock_quantity': new_stock_quantity
            })

        except Exception as e:
            error_str = str(e)
            # Handle edge case where RPC errors but operation may have succeeded
            if "'success': True" in error_str or "'message': 'Item reserved successfully!'" in error_str:
                _product_id = int(request.POST.get('product_id', 0))
                _new_stock = 0 # Default stock in this edge case
                try: 
                    stock_response = supabase.table('products').select('stock_quantity').eq('id', _product_id).single().execute()
                    if stock_response.data: _new_stock = stock_response.data.get('stock_quantity', 0)
                except: pass
                return JsonResponse({
                    'success': True,
                    'message': '✅ Your reservation has been placed successfully! (Stock may be outdated)',
                    'product_id': _product_id,
                    'new_stock_quantity': _new_stock
                })
            else:
                # Genuine error
                return JsonResponse({'success': False, 'error': f"Could not reserve item: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request.'}, status=400)

@student_required
def create_order_view(request):
    """
    Processes AJAX POST request to create a new order (direct purchase).
    
    Calls Supabase RPC to process a product purchase with payment and delivery details.
    Handles success responses and edge cases, fetches updated stock quantity,
    and returns order confirmation with current inventory status.
    Returns JSON with success status and new stock information.
    """
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            product_id = int(request.POST.get('product_id'))
            quantity_ordered = int(request.POST.get('quantity'))
            
            params = {
                'p_product_id': product_id,
                'p_user_id': request.user.id,
                'p_quantity': quantity_ordered,
                'p_deal_method': request.POST.get('deal_method'),
                'p_payment_method': request.POST.get('payment_method'),
                'p_payment_transaction_id': request.POST.get('payment_transaction_id', None)
            }

            response = supabase.rpc('buy_product', params).execute()

            if hasattr(response, 'error') and response.error:
                raise Exception(str(response.error))
            if isinstance(response.data, list) and len(response.data) > 0 and 'error' in response.data[0]:
                raise Exception(response.data[0]['error'])

            # Get the new stock quantity
            new_stock_quantity = None
            # Option A: RPC returns the new stock (Ideal)
            if isinstance(response.data, list) and len(response.data) > 0 and 'new_stock_quantity' in response.data[0]:
                new_stock_quantity = response.data[0]['new_stock_quantity']
            # Option B: Fetch manually if RPC doesn't return it
            if new_stock_quantity is None:
                stock_response = supabase.table('products').select('stock_quantity').eq('id', product_id).single().execute()
                if stock_response.data:
                    new_stock_quantity = stock_response.data.get('stock_quantity')

            if new_stock_quantity is None:
                new_stock_quantity = 0 # Default to 0 if fetch failed

            return JsonResponse({
                'success': True,
                'message': '🎉 Your order has been placed successfully!',
                'product_id': product_id,
                'new_stock_quantity': new_stock_quantity
            })

        except Exception as e:
            error_str = str(e)
            # Handle edge case where RPC errors but operation may have succeeded
            if "'success': True" in error_str:
                _product_id = int(request.POST.get('product_id', 0)) 
                _new_stock = 0
                try:
                    stock_response = supabase.table('products').select('stock_quantity').eq('id', _product_id).single().execute()
                    if stock_response.data: _new_stock = stock_response.data.get('stock_quantity', 0)
                except: pass 
                return JsonResponse({
                    'success': True,
                    'message': '🎉 Your order has been placed successfully! (Stock may be outdated)',
                    'product_id': _product_id,
                    'new_stock_quantity': _new_stock
                })
            else:
                # Genuine error
                return JsonResponse({'success': False, 'error': f"Could not place order: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request.'}, status=400)

@student_required
def checkout_reservation_view(request):
    """
    Converts a reservation into a pending order via AJAX POST request.
    
    Processes checkout of an existing reservation by calling the RPC function
    to transition it to order status. Ensures the reservation belongs to the authenticated user.
    Returns JSON confirmation of successful checkout.
    """
    if request.method == 'POST':
        try:
            reservation_id = int(request.POST.get('reservation_id'))
            user_id = request.user.id
            params = {'p_order_id': reservation_id, 'p_user_id': user_id}

            response = supabase.rpc('checkout_reservation', params).execute()
            
            # You might add error checking here based on the 'response' object
            
            return JsonResponse({'success': True, 'message': '✅ Checkout successful! Your reservation is now an order.'})

        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Could not process checkout: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=400)


@student_required
def student_profile_view(request):
    """
    Displays student profile page (GET) and handles profile/password updates (POST AJAX).
    
    For GET requests: Shows user's profile information including avatar, contact details, and address.
    For POST requests with form_type='details': Updates profile information and avatar image in storage.
    For POST requests with form_type='password': Validates current password, updates to new password,
    and logs user out with redirect to login page.
    Returns HTML page for GET, JSON responses for AJAX POST requests.
    """
    user_id = request.user.id
    
    # --- Handle AJAX POST request ---
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        form_type = request.POST.get('form_type')

        # --- Handle Profile Details Update ---
        if form_type == 'details':
            try:
                avatar_url = None
                avatar_file = request.FILES.get('avatar_image')

                if avatar_file:
                    # Create a unique file name
                    file_ext = avatar_file.name.split('.')[-1]
                    file_name = f'user_{user_id}_{uuid.uuid4()}.{file_ext}'

                    # Upload to Supabase Storage
                    supabase_service.storage.from_('avatars').upload(
                        file=avatar_file.read(),
                        path=file_name,
                        file_options={"content-type": avatar_file.content_type}
                    )

                    # Get the public URL
                    avatar_url = supabase_service.storage.from_('avatars').get_public_url(file_name)

                # Prepare parameters for the RPC function
                params = {
                    'p_full_name': request.POST.get('full_name'),
                    'p_phone_number': request.POST.get('phone_number'),
                    'p_address': request.POST.get('address')
                }

                # Add avatar_url to params ONLY if a new one was uploaded
                if avatar_url:
                    params['p_avatar_url'] = avatar_url

                # Call the RPC function
                supabase.rpc('update_my_profile', params).execute()

                # Send back the new avatar_url in the success message
                response_data = {
                    'success': True, 
                    'message': '✅ Your profile has been updated successfully!'
                }
                if avatar_url:
                    response_data['avatar_url'] = avatar_url

                return JsonResponse(response_data)

            except Exception as e:
                return JsonResponse({'success': False, 'error': f'Error updating profile: {e}'}, status=400)

        # --- Handle Password Change ---
        elif form_type == 'password':
            current_password = request.POST.get('current_password')
            new_password1 = request.POST.get('new_password1')
            new_password2 = request.POST.get('new_password2')

            # --- Backend validation ---
            if new_password1 != new_password2:
                return JsonResponse({'success': False, 'error': 'Passwords do not match.'}, status=400)
            if len(new_password1) < 8: 
                return JsonResponse({'success': False, 'error': 'Password must be at least 8 characters long.'}, status=400)
            if current_password == new_password1:
                return JsonResponse({'success': False, 'error': 'New password cannot be the same as the current password.'}, status=400)

            try:
                # 1. VALIDATE CURRENT PASSWORD
                user_email = request.user.email
                supabase.auth.sign_in_with_password({
                    "email": user_email,
                    "password": current_password
                })

                # 2. UPDATE TO NEW PASSWORD
                supabase.auth.update_user({'password': new_password1})

                # 3. MANUALLY LOG THE USER OUT (Clear invalid tokens)
                if 'supa_access_token' in request.session:
                    del request.session['supa_access_token']
                if 'supa_refresh_token' in request.session:
                    del request.session['supa_refresh_token']
                
                # 4. PREPARE A CLEAN REDIRECT
                # This message will show up on the login page after redirect
                messages.success(request, 'Your password has been changed successfully! Please log in again.')
                
                # Get the URL for the login page
                login_url = reverse('login') 

                # 5. RETURN a JSON response telling the frontend to redirect
                return JsonResponse({
                    'success': True, 
                    'message': '🔑 Your password has been changed successfully!',
                    'redirect_url': login_url  # <-- Your JavaScript needs to handle this
                })

            except requests.exceptions.HTTPError as e:
                # CATCH AUTHENTICATION ERROR
                if "Invalid login credentials" in str(e):
                    return JsonResponse({'success': False, 'error': 'Your current password was incorrect.'}, status=400)
                else:
                    return JsonResponse({'success': False, 'error': f'An error occurred: {e}'}, status=400)
            except Exception as e:
                return JsonResponse({'success': False, 'error': f'Error changing password: {e}'}, status=400)

        # Fallback for unknown form type
        return JsonResponse({'success': False, 'error': 'Invalid form submission.'}, status=400)

    # --- Handle GET request to display the page ---
    profile_data = {}
    try:
        response = supabase.table('user_profiles').select('*').eq('user_id', user_id).single().execute()
        profile_data = response.data
    except Exception as e:
        messages.error(request, f'Could not load your profile: {e}')
        
    context = {
        'profile': profile_data,
        'active_page': 'profile',
        'page_title': 'My Profile'
    }
    return render(request, 'dashboards/student_profile.html', context)

@student_required
def cancel_reservation_view(request, reservation_id):
    """
    Allows students to cancel their own reservation via AJAX POST request.

    Uses Supabase RPC to mark the specified reservation as cancelled.
    Ownership enforcement relies on RLS rules. Responds with JSON to confirm
    successful cancellation or return appropriate error messages.
    """

    if request.method == 'POST':
        try:
            # Utilize the admin's shared cancel/reject RPC for processing
            supabase.rpc(
                'cancel_or_reject_order',
                {
                    'p_order_id': reservation_id,
                    'p_new_status': 'cancelled'
                }
            ).execute()

            return JsonResponse({
                'success': True,
                'message': '✅ Your reservation has been successfully cancelled.'
            })

        except Exception as e:
            return JsonResponse(
                {
                    'success': False,
                    'error': f"Could not cancel the reservation: {e}"
                },
                status=400
            )

    return JsonResponse(
        {
            'success': False,
            'error': 'Invalid request method.'
        },
        status=400
    )

@student_required
def cancel_order_view(request, order_id):
    """
    Allows students to cancel approved orders via AJAX POST request.
    
    Verifies the order is approved and belongs to the authenticated student before cancellation.
    Calls RPC to cancel order and restore product stock. Includes ownership validation.
    Returns JSON response confirming cancellation or error details.
    """

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            user_id = request.user.id

            # Ensure the order is 'approved' and belongs to the user
            response = (
                supabase.table('orders')
                .select('id')
                .eq('id', order_id)
                .eq('user_id', user_id)
                .eq('status', 'approved')
                .single()
                .execute()
            )

            if not response.data:
                raise Exception("Order not found or cannot be cancelled.")

            # Call RPC to cancel and restore stock
            supabase.rpc(
                'cancel_or_reject_order',
                {'p_order_id': order_id, 'p_new_status': 'cancelled'}
            ).execute()

            return JsonResponse({
                'success': True,
                'message': "✅ Your order has been successfully cancelled.",
                'order_id': order_id
            })

        except Exception as e:
            return JsonResponse(
                {'success': False, 'error': f"Could not cancel the order: {e}"},
                status=400
            )

    return JsonResponse(
        {'success': False, 'error': 'Invalid request.'},
        status=400
    )

 

# --- Admin Views ---

@admin_required
def admin_dashboard(request):
    """
    Renders the main admin dashboard with greeting and key performance statistics.
    
    Displays personalized greeting, admin name, and dashboard statistics (total products,
    orders, active products, pending orders) fetched via RPC. Provides at-a-glance overview
    of system status and key metrics.
    """
    raw_name = getattr(request.user, 'get_full_name', lambda: 'Admin')()
    display_name = raw_name.split()[0] if isinstance(raw_name, str) else "Admin"
    
    stats = {}
    try:
        stats = supabase.rpc('get_dashboard_stats').execute().data
    except Exception as e:
        messages.error(request, f"Could not load dashboard stats: {e}")
        stats = {'total_products': 0, 'total_orders': 0, 'active_products': 0, 'pending_orders': 0}

    context = {
        'display_name': display_name, 'greeting': get_greeting(),
        'stats': stats, 'active_page': 'dashboard',
    }
    return render(request, 'dashboards/admin_dashboard.html', context)

@admin_required
def manage_products_view(request):
    """
    Displays product management interface for admins with search and filtering.
    
    Fetches all products with optional keyword search across name and category.
    Sorts products to highlight unavailable items and low-stock products first.
    Provides comprehensive product list for management operations.
    """
    search_query = request.GET.get('search', '').strip()
    page_number = request.GET.get('page', 1) 
    items_per_page = 15

    products = []
    total_products_count = 0
    pagination_context = {}
    
    try:
        query = supabase.table('products').select('*').order('created_at', desc=True)
        if search_query:
            query = query.or_(f'name.ilike.%{search_query}%,category.ilike.%{search_query}%')
        response = query.execute()
        products = response.data if response.data else []

        # Sort products to show unavailable and low stock items first
        products = sorted(
            products, 
            key=lambda p: (
                p.get('is_available', True), # Sorts False (Unavailable) before True (Available)
                not (0 < p.get('stock_quantity', 0) < 10) # Sorts True (Low Stock) before False
            )
        )

        categories = sorted(list(set(p.get('category') for p in products if p.get('category'))))

    except Exception as e:
        messages.error(request, f"Error fetching products: {e}")
        products = []
    context = {
        'products': products, 
        'categories': categories,
        'active_page': 'manage_products',
        'page_title': 'Manage Products',
    }
    return render(request, 'dashboards/manage_products.html', context)

@admin_required
def add_product(request):
    """
    Handles AJAX POST request for admin to add a new product.
    
    Processes product creation including image upload to Supabase storage,
    and handles size field for Uniforms category. Validates all input data
    and logs the activity. Returns JSON with new product details.
    """
    if request.method == 'POST':
        try:
            image_url = None
            image_file = request.FILES.get('product-image')
            
            if image_file:
                file_ext = image_file.name.split('.')[-1]
                file_name = f'product_{uuid.uuid4()}.{file_ext}'
                
                supabase_service.storage.from_('product_images').upload(
                    file=image_file.read(), 
                    path=file_name, 
                    file_options={"content-type": image_file.content_type}
                )
                image_url = supabase_service.storage.from_('product_images').get_public_url(file_name)
            
            stock = int(request.POST.get('stock-quantity', 0))

            product_data = {
                'name': request.POST.get('product-name'),
                'description': request.POST.get('product-description'),
                'price': float(request.POST.get('product-price')),
                'stock_quantity': int(request.POST.get('stock-quantity')),
                'category': request.POST.get('product-category'),
                'image_url': image_url,
                'is_available': stock > 0
            }

            if request.POST.get('product-category') == 'Uniforms':
                product_data['size'] = request.POST.get('product-size')

            response = supabase_service.table('products').insert(product_data).execute()
            
            if not response.data or len(response.data) == 0:
                raise Exception("Failed to create product, no data returned.")

            new_product = response.data[0]
            
            log_activity(
                request.user,
                'PRODUCT_ADDED',
                {'product_name': new_product['name'], 'product_id': new_product['id']}
            )
            
            return JsonResponse({'success': True, 'message': 'Product added successfully!', 'product': new_product})

        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Failed to add product: {e}"}, status=400)
            
    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)


@admin_required
def edit_product(request, product_id):
    """
    Handles AJAX POST request for admin to update an existing product.
    
    Processes product updates including price, stock, category, and optional image replacement.
    Tracks changes made for activity logging. Handles old image deletion when new image uploaded.
    Returns JSON with updated product details.
    """
    if request.method == 'POST':
        try:
            # Fetch the *old* product data first for comparison and logging
            old_product_response = supabase_service.table('products').select('*').eq('id', product_id).single().execute()
            if not old_product_response.data:
                raise Exception("Product to edit not found.")
            old_product = old_product_response.data

            category = request.POST.get('product-category')
            stock = int(request.POST.get('stock-quantity'))
            price = float(request.POST.get('product-price'))
            name = request.POST.get('product-name')
            description = request.POST.get('product-description')

            update_data = {
                'name': name,
                'description': description,
                'price': price,
                'stock_quantity': stock,
                'category': category, 
                'is_available': stock > 0
            }

            if category == 'Uniforms':
                update_data['size'] = request.POST.get('product-size')
            else:
                update_data['size'] = None 
            
            new_image_file = request.FILES.get('product-image')
            if new_image_file:
                # Upload new image
                file_ext = new_image_file.name.split('.')[-1]
                file_name = f'product_{uuid.uuid4()}.{file_ext}'
                
                supabase_service.storage.from_('product_images').upload(
                    file=new_image_file.read(), 
                    path=file_name, 
                    file_options={"content-type": new_image_file.content_type}
                )
                
                new_image_url = supabase_service.storage.from_('product_images').get_public_url(file_name)
                update_data['image_url'] = new_image_url

                # Delete old image
                old_image_url = request.POST.get('current-image-url')
                if old_image_url and old_image_url.strip():
                    try:
                        old_file_name = old_image_url.split('/')[-1]
                        supabase_service.storage.from_('product_images').remove([old_file_name])
                    except Exception as e:
                        print(f"Could not remove old image '{old_file_name}': {e}")
            
            update_response = supabase_service.table('products').update(update_data).eq('id', product_id).execute()

            if not update_response.data or len(update_response.data) == 0:
                raise Exception("Failed to update product, no data returned from update.")
            
            updated_product = update_response.data[0]
            
            # Compare old and new values to build a list of changes for logging
            changes = []
            if old_product.get('name') != name:
                changes.append(f"Name changed to '{name}'")
            if float(old_product.get('price', 0)) != price:
                changes.append(f"Price changed from ₱{old_product.get('price', 0)} to ₱{price}")
            if int(old_product.get('stock_quantity', 0)) != stock:
                changes.append(f"Stock changed from {old_product.get('stock_quantity', 0)} to {stock}")
            if old_product.get('category') != category:
                changes.append(f"Category changed to '{category}'")
            if new_image_file:
                changes.append("Image was updated")
            
            if not changes:
                changes = ["Details updated"] # Fallback

            log_activity(
                request.user,
                'PRODUCT_EDITED',
                {
                    'product_name': updated_product['name'], 
                    'product_id': product_id,
                    'changes': ", ".join(changes) 
                }
            )
            
            return JsonResponse({'success': True, 'message': 'Product updated successfully!', 'product': updated_product})

        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Failed to update product: {e}"}, status=400)
            
    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

@admin_required
def delete_product(request, product_id):
    """
    Handles AJAX POST request for admin to permanently delete a product.
    
    Removes a product from the database and logs the deletion activity.
    Fetches product name before deletion for meaningful audit logging.
    Returns JSON confirmation with deleted product ID.
    """
    if request.method == 'POST':
        try:
            product_name = 'Unknown'
            # Get product name before deleting for logging
            response = supabase_service.table('products').select('name').eq('id', product_id).execute()
            if response.data:
                product_name = response.data[0]['name']
                
            supabase_service.table('products').delete().eq('id', product_id).execute()
            
            log_activity(request.user, 'PRODUCT_DELETED', {'product_id': product_id, 'product_name': product_name})
            
            return JsonResponse({
                'success': True, 
                'message': 'Product deleted successfully!', 
                'product_id': product_id # Send the ID back to the JS
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Failed to delete product: {e}"}, status=400)
            
    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)


@admin_required
def order_management_view(request):
    """
    Displays all orders organized by status for admin management.
    
    Fetches orders using RPC with optional search filtering. Categorizes into
    pending, approved, completed, and other (cancelled/rejected) statuses.
    Handles date parsing and provides comprehensive order overview.
    """
    search_query = request.GET.get('search', '').strip()
    
    pending_orders = [] 
    approved_orders = []
    completed_orders = []
    other_orders = [] # This will hold cancelled/rejected
    all_orders_empty = True
    
    try:
        params = {'p_search_term': search_query}
        response = supabase_service.rpc('get_all_orders_with_details', params).execute()
        
        if response.data:
            all_orders_empty = False
            for item in response.data:
                # --- Parse created_at ---
                if item.get('created_at'):
                    try:
                        item['created_at'] = datetime.fromisoformat(item['created_at'])
                    except (ValueError, TypeError):
                        item['created_at'] = None

                # --- Add expires_at parsing ---
                if item.get('expires_at'):
                    try:
                        item['expires_at'] = datetime.fromisoformat(item['expires_at'])
                    except (ValueError, TypeError):
                        item['expires_at'] = None
                else:
                    item['expires_at'] = None
                
                status = item.get('status')
                
                if status == 'pending':
                    pending_orders.append(item)
                elif status == 'approved':
                    approved_orders.append(item)
                elif status == 'completed':
                    completed_orders.append(item)
                elif status in ['cancelled', 'rejected']:
                    other_orders.append(item)
                            
    except Exception as e:
        messages.error(request, f"An error occurred while fetching orders: {e}")
        all_orders_empty = True

    context = {
        'pending_orders': pending_orders,
        'approved_orders': approved_orders,
        'completed_orders': completed_orders,
        'other_orders': other_orders,
        'search_query': search_query,
        'active_page': 'order_management',
        'page_title': 'Order Management',
        'all_orders_empty': all_orders_empty
    }
    return render(request, 'dashboards/order_management.html', context)

@admin_required
def admin_batch_delete_orders_view(request):
    """ 
    Allows admins to permanently delete multiple orders via AJAX.
    
    Processes batch deletion of selected orders and logs the activity.
    Validates order IDs and ensures deletion is recorded in audit trail.
    Returns JSON confirmation with count and details of deleted orders.
    """
    if request.method == 'POST':
        order_ids_str = request.POST.get('order_ids')
        
        if not order_ids_str:
            return JsonResponse({'success': False, 'error': "No orders selected for deletion."}, status=400)

        order_ids = [int(oid) for oid in order_ids_str.split(',') if oid.isdigit()]
        
        if not order_ids:
            return JsonResponse({'success': False, 'error': "Invalid order IDs provided."}, status=400)

        try:
            log_activity(
                request.user,
                'ORDER_BATCH_DELETED',
                {'count': len(order_ids), 'order_ids': order_ids}
            )

            supabase_service.table('orders').delete().in_('id', order_ids).execute()
            
            return JsonResponse({
                'success': True, 
                'message': f"{len(order_ids)} has been permanently deleted.",
                'order_ids': order_ids 
            })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': f"An error occurred during batch deletion: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request.'}, status=400)

@admin_required
def batch_update_products(request):
    """
    Handles batch operations on multiple products via POST request.
    
    Supports multiple actions: 'mark-available' (standard form submission)
    and 'delete-selected' (AJAX request). Validates product IDs and logs all activities.
    Returns appropriate response based on action type (redirect or JSON).
    """
    if request.method == 'POST':
        action = request.POST.get('action')
        product_ids_str = request.POST.get('product_ids')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        if not all([action, product_ids_str]):
            if is_ajax:
                return JsonResponse({'success': False, 'error': 'Invalid batch action request.'}, status=400)
            messages.error(request, "Invalid batch action request.")
            return redirect('manage_products')

        try:
            product_ids = [int(pid) for pid in product_ids_str.split(',') if pid.isdigit()]
            if not product_ids:
                raise ValueError("No valid product IDs provided.")
            count = len(product_ids)

        except ValueError as e:
            if is_ajax:
                return JsonResponse({'success': False, 'error': f"Invalid product IDs: {e}"}, status=400)
            messages.error(request, f"Invalid product IDs: {e}")
            return redirect('manage_products')

        try:
            if action == 'mark-available':
                supabase_service.table('products').update({'is_available': True}).in_('id', product_ids).execute()
                messages.success(request, f"{count} product(s) marked as available.")
                log_activity(
                    request.user, 
                    f'PRODUCT_BATCH_{action.upper().replace("-", "_")}',
                    {'count': count, 'product_ids': product_ids}
                )
                return redirect('manage_products')

            elif action == 'delete-selected':
                supabase_service.table('products').delete().in_('id', product_ids).execute()
                
                log_activity(
                    request.user, 
                    'PRODUCT_BATCH_DELETE',
                    {'count': count, 'product_ids': product_ids}
                )
                
                # This action expects an AJAX response
                return JsonResponse({
                    'success': True, 
                    'message': 'Product deleted successfully!',
                    'product_ids': product_ids
                })
                
            else:
                messages.warning(request, f"The batch action '{action}' is not supported.")

        except Exception as e:
            if is_ajax:
                return JsonResponse({'success': False, 'error': f"An error occurred: {e}"}, status=400)
            messages.error(request, f"An error occurred during the batch {action}: {e}")
    
    return redirect('manage_products')

@admin_required
def update_order_status(request, order_id):
    """
    Handles AJAX POST request for admin to update order status (single or batch).
    
    Supports single order updates via order_id parameter or batch updates via POST data.
    Handles special logic for cancelled/rejected orders (stock restoration via RPC)
    and expiration date management for approved/completed orders. Logs all changes.
    Returns JSON with updated order data and confirmation message.
    """
    if request.method == 'POST':
        try:
            order_ids = []
            new_status = request.POST.get('status')
            
            is_batch = order_id == 0

            if is_batch:
                order_ids_str = request.POST.get('order_ids')
                if not order_ids_str:
                    raise ValueError("No order IDs provided for batch update.")
                order_ids = [int(oid) for oid in order_ids_str.split(',') if oid.isdigit()]
            else:
                order_ids = [order_id]

            if not order_ids or not new_status:
                raise ValueError("Missing order IDs or new status.")

            # For logging, fetch details *before* we change them.
            # We'll just fetch details for the first order in the batch.
            log_details = {'order_ids': order_ids, 'new_status': new_status}
            try:
                first_order_id = order_ids[0]
                log_order_res = supabase_service.table('orders') \
                    .select('user_id, product_id, user_profiles(full_name), products(name)') \
                    .eq('id', first_order_id) \
                    .single() \
                    .execute()
                    
                if log_order_res.data:
                    log_order_data = log_order_res.data
                    log_details['product_name'] = log_order_data.get('products', {}).get('name', 'N/A')
                    log_details['student_name'] = log_order_data.get('user_profiles', {}).get('full_name', 'N/A')
                    if is_batch:
                        log_details['count'] = len(order_ids)
                    else:
                        log_details['order_id'] = first_order_id
            except Exception as e:
                print(f"Error pre-fetching order details for logging: {e}")

            # --- Start of the update logic ---
            if new_status in ['cancelled', 'rejected']:
                # Use RPC to handle stock restoration
                for oid in order_ids:
                    supabase_service.rpc('cancel_or_reject_order', {
                        'p_order_id': oid,
                        'p_new_status': new_status
                    }).execute()
            else:
                # Build the dictionary of what to update
                update_data = {'status': new_status}
                new_status_lower = new_status.lower()

                if new_status == 'approved':
                    # SET a new 3-day expiration date from right now
                    expires_at_date = datetime.now(timezone.utc) + timedelta(days=3)
                    update_data['expires_at'] = expires_at_date.isoformat()
                
                elif new_status == 'completed':
                    # CLEAR the expiration date, as it's no longer needed
                    update_data['expires_at'] = None
                
                # Note: If new_status is 'pending', we do nothing,
                # which preserves the original reservation expiration date.

                # Perform the update
                supabase_service.table('orders').update(
                    update_data
                ).in_('id', order_ids).execute()

            # After updating, re-fetch the orders to get fresh data
            updated_orders_response = supabase_service.table('orders') \
                                        .select('*') \
                                        .in_('id', order_ids) \
                                        .execute()
            
            updated_orders_data = updated_orders_response.data if updated_orders_response.data else []
            
            # Log the activity
            log_activity(
                request.user,
                'ORDER_STATUS_BATCH_UPDATED' if is_batch else 'ORDER_STATUS_UPDATED',
                log_details 
            )

            return JsonResponse({
                'success': True,
                'message': f"{len(updated_orders_data)} order(s) updated to '{new_status}'.",
                'orders': updated_orders_data
            })

        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Failed to update order status: {e}"}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

@admin_required
def delete_order_view(request, order_id):
    """
    Handles AJAX POST request for admin to permanently delete a single order.
    
    Fetches order details before deletion for comprehensive audit logging including
    product name and student information. Logs the deletion activity.
    Returns JSON confirmation with deleted order ID.
    """
    if request.method == 'POST':
        
        # Get order details *before* we delete it for logging.
        log_details = {'order_id': order_id}
        try:
            log_order_res = supabase_service.table('orders') \
                .select('user_id, product_id, user_profiles(full_name, student_id), products(name)') \
                .eq('id', order_id) \
                .single() \
                .execute()
            
            if log_order_res.data:
                log_order_data = log_order_res.data
                log_details['product_name'] = log_order_data.get('products', {}).get('name', 'N/A')
                log_details['student_name'] = log_order_data.get('user_profiles', {}).get('full_name', 'N/A')
                log_details['student_id_num'] = log_order_data.get('user_profiles', {}).get('student_id', 'N/A')
        except Exception as e:
            print(f"Error pre-fetching order details for logging: {e}")

        try:
            # Perform the deletion
            supabase_service.table('orders').delete().eq('id', order_id).execute()
            
            # Log the activity with the full details
            log_activity(request.user, 'ORDER_DELETED', log_details)
            
            return JsonResponse({
                'success': True, 
                'message': f'Order #{order_id} has been permanently deleted.',
                'order_id': order_id
            })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': f"Failed to delete order: {e}"}, status=400)
            
    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)


@admin_required
def reports_view(request):
    """
    Displays comprehensive system reports with KPIs, inventory, sales, and activity logs.
    
    Fetches advanced report statistics via RPC including dashboard KPIs, inventory overview,
    reservation statistics, and sales performance. Implements paginated activity log display
    with 10 entries per page. Identifies and highlights low-stock and unavailable products.
    Provides multi-faceted reporting for business intelligence.
    """
    search_query = request.GET.get('search', '').strip()
    log_page_number = request.GET.get('log_page', 1) 
    logs_per_page = 10
    report_data = {}
    kpi_data = {}
    inventory_overview = {} 
    reservation_stats = {} 
    sales_performance = {} 
    log_entries = []
    low_stock_products = []
    unavailable_products = []
    pending_backorders_count = 0
    status_counts_dict = {
        'pending': 0, 'approved': 0, 'completed': 0, 'rejected': 0, 'cancelled': 0
    }

    log_pagination_context = {} 
    total_log_count = 0

    try:
        # Call the advanced RPC function
        report_data_response = supabase_service.rpc('get_advanced_report_stats').execute()

        if report_data_response.data:
            report_data = report_data_response.data

            # Extract data from the single JSON object response
            retrieved_status_counts = report_data.get('status_counts', {})
            status_counts_dict.update(retrieved_status_counts)
            kpi_data = report_data.get('kpi', {})
            inventory_overview = report_data.get('inventory_overview', {})
            reservation_stats = report_data.get('reservation_stats', {})
            sales_performance = report_data.get('sales_performance', {})

        else:
            # Set defaults if RPC fails
            report_data = {'total_products': 0, 'total_orders_reservations': 0}
            kpi_data = {'total_sales': 0, 'inventory_value': 0, 'orders_today': 0, 'pending_reservations': 0}

        # --- Add new query for pending backorders ---
        try:
            backorder_count_response = supabase_service.table('orders') \
                .select('id', count='exact') \
                .eq('status', 'pending') \
                .eq('order_type', 'backorder') \
                .execute()
            if backorder_count_response.count is not None:
                pending_backorders_count = backorder_count_response.count
        except Exception as e:
            print(f"Error fetching backorder count: {e}")
            pending_backorders_count = 0
        # --- End of new query ---

        # --- Fetch supporting product lists ---
        low_stock_response = supabase_service.table('products').select('*').gt('stock_quantity', 0).lt('stock_quantity', 10).order('stock_quantity', desc=False).execute()
        if low_stock_response.data:
            low_stock_products = low_stock_response.data

        unavailable_response = supabase_service.table('products').select('*').eq('is_available', False).order('name').execute()
        if unavailable_response.data:
            unavailable_products = unavailable_response.data

        # --- Fetch Paginated Log Data ---
        try:
            log_page_number = int(log_page_number)
        except ValueError:
            log_page_number = 1

        log_start_index = (log_page_number - 1) * logs_per_page
        log_end_index = log_start_index + logs_per_page - 1

        # 1. Get the total count from the TABLE
        count_response = supabase_service.table('activity_log').select(
            'id', count='exact'
        ).execute()
        total_log_count = count_response.count if count_response.count is not None else 0

        # 2. Fetch the paginated data using the RPC
        log_query = supabase_service.rpc(
            'get_activity_log',
            {'p_search_term': ''} # Keep search empty for client-side filtering
        ).order('created_at', desc=True).range(log_start_index, log_end_index) 

        log_response = log_query.execute()

        if log_response.data:
            for entry in log_response.data:
                if entry.get('created_at'):
                        entry['created_at'] = datetime.fromisoformat(entry['created_at'])
                if entry.get('action'):
                        entry['action_display'] = entry['action'].replace('_', ' ').title()
                log_entries.append(entry)

        # Calculate Log Pagination Details
        total_log_pages = math.ceil(total_log_count / logs_per_page)
        log_pagination_context = {
            'current_page': log_page_number,
            'total_pages': total_log_pages,
            'has_previous': log_page_number > 1,
            'has_next': log_page_number < total_log_pages,
            'previous_page_number': log_page_number - 1 if log_page_number > 1 else 1,
            'next_page_number': log_page_number + 1 if log_page_number < total_log_pages else total_log_pages,
            'page_range': range(max(1, log_page_number - 2), min(total_log_pages, log_page_number + 2) + 1),
            'param_name': 'log_page'
        }

    except Exception as e:
        messages.error(request, f"Error fetching report data: {e}")
        report_data = {'total_products': 0, 'total_orders_reservations': 0}
        kpi_data = {'total_sales': 0, 'inventory_value': 0, 'orders_today': 0, 'pending_reservations': 0}
        log_entries = []
        total_log_count = 0

    context = {
        'total_products': report_data.get('total_products', 0),
        'total_orders_reservations': report_data.get('total_orders_reservations', 0),
        'status_counts': status_counts_dict,
        'kpi': kpi_data,
        'inventory_overview': inventory_overview,
        'reservation_stats': reservation_stats,
        'sales_performance': sales_performance,
        'log_entries': log_entries,
        'low_stock_products': low_stock_products,
        'unavailable_products': unavailable_products,
        'search_query': search_query,
        'log_pagination': log_pagination_context,
        'active_page': 'reports',
        'page_title': 'System Reports',
        'pending_backorders_count': pending_backorders_count
    }
    return render(request, 'dashboards/reports.html', context)

@admin_required
def batch_delete_logs_view(request):
    """
    Handles AJAX POST request for admin to batch delete activity log entries.
    
    Processes deletion of multiple log records by IDs. Validates input and removes
    entries from the activity_log table. Returns JSON with deletion count and confirmation.
    """
    if request.method == 'POST':
        log_ids_str = request.POST.get('log_ids')
        if not log_ids_str:
            return JsonResponse({'success': False, 'error': 'No log IDs provided.'}, status=400)
            
        try:
            log_ids = [int(lid) for lid in log_ids_str.split(',') if lid.isdigit()]
            if not log_ids:
                raise ValueError("No valid log IDs provided.")
            
            supabase_service.table('activity_log').delete().in_('id', log_ids).execute()
            
            return JsonResponse({
                'success': True,
                'message': f'{len(log_ids)} log entries have been deleted.',
                'deleted_ids': log_ids
            })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': f'An error occurred: {e}'}, status=500)
    
    return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

@require_POST
@admin_required
def clear_all_logs_view(request):
    """
    Handles AJAX POST request for admin to delete ALL activity log entries.
    
    Performs complete purge of the activity_log table. Counts entries before deletion
    and logs this destructive action for security audit trail purposes.
    Returns JSON confirmation with count of cleared entries.
    """
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=400)

    try:
        # 1. Get a count of logs to be deleted for the log message
        count_response = supabase_service.table('activity_log').select('id', count='exact').execute()
        log_count = count_response.count
        
        # 2. Delete all rows
        supabase_service.table('activity_log').delete().neq('id', 0).execute()

        # 3. Log this action *after* clearing.
        log_activity(
            request.user,
            'CLEAR_ALL_LOGS',
            {'count': log_count}
        )
        
        return JsonResponse({
            'success': True, 
            'message': f'✅ Successfully cleared all {log_count} log entries.'
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@admin_required
def admin_profile_view(request):
    """
    Displays admin profile page (GET) and handles profile/password updates (POST AJAX).
    
    For GET requests: Shows admin's profile information including avatar, contact details, and address.
    For POST requests with form_type='details': Updates profile and avatar image in storage.
    For POST requests with form_type='password': Validates current password, updates password,
    and logs user out with redirect to login page.
    Returns HTML page for GET, JSON responses for AJAX POST requests.
    """
    user_id = request.user.id

    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        form_type = request.POST.get('form_type')

        # --- Handle Profile Details Update ---
        if form_type == 'details':
            try:
                avatar_url = None
                avatar_file = request.FILES.get('avatar_image')

                if avatar_file:
                    file_ext = avatar_file.name.split('.')[-1]
                    file_name = f'user_{user_id}_{uuid.uuid4()}.{file_ext}'

                    # Upload to Supabase Storage (using service client)
                    supabase_service.storage.from_('avatars').upload(
                        file=avatar_file.read(),
                        path=file_name,
                        file_options={"content-type": avatar_file.content_type}
                    )

                    avatar_url = supabase_service.storage.from_('avatars').get_public_url(file_name)

                params = {
                    'p_full_name': request.POST.get('full_name'),
                    'p_phone_number': request.POST.get('phone_number'),
                    'p_address': request.POST.get('address')
                }

                if avatar_url:
                    params['p_avatar_url'] = avatar_url

                # Call RPC (using user's auth)
                supabase.rpc('update_my_profile', params).execute()

                response_data = {
                    'success': True, 
                    'message': '✅ Your profile has been updated successfully!'
                }
                if avatar_url:
                    response_data['avatar_url'] = avatar_url

                return JsonResponse(response_data)

            except Exception as e:
                return JsonResponse({'success': False, 'error': f'Error updating profile: {e}'}, status=400)

        # Handle Password Change
        elif form_type == 'password':
            current_password = request.POST.get('current_password')
            new_password1 = request.POST.get('new_password1')
            new_password2 = request.POST.get('new_password2')

            # --- Backend validation ---
            if new_password1 != new_password2:
                return JsonResponse({'success': False, 'error': 'Passwords do not match.'}, status=400)
            if len(new_password1) < 8: 
                return JsonResponse({'success': False, 'error': 'Password must be at least 8 characters long.'}, status=400)
            if current_password == new_password1:
                return JsonResponse({'success': False, 'error': 'New password cannot be the same as the current password.'}, status=400)

            try:
                # 1. VALIDATE CURRENT PASSWORD
                user_email = request.user.email
                supabase.auth.sign_in_with_password({
                    "email": user_email,
                    "password": current_password
                })

                # 2. UPDATE TO NEW PASSWORD
                supabase.auth.update_user({'password': new_password1})

                # 3. MANUALLY LOG THE USER OUT (Clear invalid tokens)
                if 'supa_access_token' in request.session:
                    del request.session['supa_access_token']
                if 'supa_refresh_token' in request.session:
                    del request.session['supa_refresh_token']
                
                # 4. PREPARE A CLEAN REDIRECT
                messages.success(request, 'Your password has been changed successfully! Please log in again.')
                login_url = reverse('login') 

                # 5. RETURN a JSON response telling the frontend to redirect
                return JsonResponse({
                    'success': True, 
                    'message': '🔑 Your password has been changed successfully!',
                    'redirect_url': login_url  # <-- Your JavaScript needs to handle this
                })

            except requests.exceptions.HTTPError as e:
                # CATCH AUTHENTICATION ERROR
                if "Invalid login credentials" in str(e):
                    return JsonResponse({'success': False, 'error': 'Your current password was incorrect.'}, status=400)
                else:
                    return JsonResponse({'success': False, 'error': f'An error occurred: {e}'}, status=400)
            except Exception as e:
                return JsonResponse({'success': False, 'error': f'Error changing password: {e}'}, status=400)

        # Fallback for unknown form type
        return JsonResponse({'success': False, 'error': 'Invalid form submission.'}, status=400)

    # --- Handle GET request ---
    profile_data = {}
    try:
        response = supabase.table('user_profiles').select('*').eq('user_id', user_id).single().execute()
        profile_data = response.data
    except Exception as e:
        messages.error(request, f'Could not load your profile: {e}')

    context = {
        'profile': profile_data,
        'active_page': 'profile',
        'page_title': 'Admin Profile'
    }
    return render(request, 'dashboards/admin_profile.html', context)


@admin_required
def manage_students_view(request):
    """
    Displays paginated student list with search and filtering for admin management.
    
    Supports both standard page load (GET) and AJAX pagination/search requests.
    Fetches paginated student data via RPC with 10 students per page. Shows student
    statistics on initial load. Returns HTML page or JSON data based on request type.
    """
    search_query = request.GET.get('search', '').strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    try:
        current_page = int(request.GET.get('page', 1))
    except ValueError:
        current_page = 1
    
    page_size = 10 
    
    stats = {}
    students = []
    total_count = 0
    total_pages = 1
    
    try:
        # 1. Get the stat card data (only on the initial page load)
        if not is_ajax:
            stats_response = supabase_service.rpc('get_student_stats').execute()
            if stats_response.data:
                stats = stats_response.data[0]
            else:
                stats = {'total_students': 0, 'blocked_students': 0}

        # 2. Get paginated student list via RPC
        params = {
            'p_search_term': search_query,
            'p_page_size': page_size,
            'p_page_number': current_page
        }
        students_response = supabase_service.rpc('get_paginated_student_profiles', params).execute()
        
        if students_response.data:
            students = students_response.data
            # Get total count from the first item (as returned by RPC)
            total_count = students[0].get('total_count', 0)
            total_pages = math.ceil(total_count / page_size)

            if not is_ajax:
                for student in students:
                    if student.get('created_at'):
                        student['created_at'] = datetime.fromisoformat(student['created_at'])
        
        if is_ajax:
            # For AJAX search/pagination, return JSON data
            return JsonResponse({
                'students': students,
                'total_count': total_count,
                'total_pages': total_pages,
                'current_page': current_page,
                'page_range': list(range(1, total_pages + 1)) 
            })

    except Exception as e:
        if is_ajax:
            return JsonResponse({'error': str(e)}, status=500)
        messages.error(request, f"Error fetching student data: {e}")
        
    # --- Render full page for initial GET request ---
    context = {
        'stats': stats,
        'students': students,
        'search_query': search_query,
        'total_count': total_count,
        'total_pages': total_pages,
        'current_page': current_page,
        'page_range': range(1, total_pages + 1),
        'active_page': 'manage_students',
        'page_title': 'Manage Student Accounts'
    }
    return render(request, 'dashboards/manage_students.html', context)


@admin_required
def admin_block_student_view(request, user_id):
    """
    Handles AJAX POST request for admin to block or unblock a student account.
    
    Toggles student's blocked status based on the is_blocked parameter.
    Fetches student details before action for detailed audit logging including
    student name and ID number. Returns JSON confirmation of status change.
    """
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        
        # Get student details *before* the action for logging
        student_name = "Unknown Student"
        student_id_num = "N/A"
        try:
            profile_res = supabase_service.table('user_profiles').select('full_name, student_id').eq('user_id', str(user_id)).single().execute()
            if profile_res.data:
                student_name = profile_res.data.get('full_name', 'Unknown Student')
                student_id_num = profile_res.data.get('student_id', 'N/A')
        except Exception as e:
            print(f"Error pre-fetching student details for logging: {e}")

        try:
            is_blocked_str = request.POST.get('is_blocked', 'false')
            is_blocked = True if is_blocked_str == 'true' else False

            params = {
                'p_user_id': str(user_id),
                'p_is_blocked': is_blocked
            }
            supabase_service.rpc('admin_update_user_status', params).execute()

            action_text = "blocked" if is_blocked else "unblocked"
            
            # Log the action with details
            log_details = {
                'student_user_id': str(user_id),
                'student_name': student_name,
                'student_id_num': student_id_num,
                'action_taken': action_text
            }
            log_activity(
                request.user, 
                'STUDENT_STATUS_UPDATED',
                log_details 
            )
            
            return JsonResponse({'success': True, 'message': f'✅ Student {student_name} has been {action_text}.'})

        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)

@require_POST
@admin_required
def admin_delete_student_view(request, user_id):
    """
    Handles AJAX POST request for admin to permanently delete a student account.
    
    Calls RPC function to delete student and all associated data (orders, reservations, etc.).
    Fetches student details before deletion for comprehensive audit logging.
    Returns JSON confirmation with deleted student ID and name.
    """
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=400)

    # Get the student's details *before* we delete them.
    student_name = "Unknown Student"
    student_id_num = "N/A"
    try:
        profile_res = supabase_service.table('user_profiles').select('full_name, student_id').eq('user_id', str(user_id)).single().execute()
        if profile_res.data:
            student_name = profile_res.data.get('full_name', 'Unknown Student')
            student_id_num = profile_res.data.get('student_id', 'N/A')
    except Exception as e:
        print(f"Error pre-fetching student details for logging: {e}")

    try:
        # Call the RPC to delete the user and all related data
        params = {'p_user_id': str(user_id)}
        supabase_service.rpc('admin_delete_student', params).execute()

        # Log this admin action
        log_details = {
            'student_user_id': str(user_id),
            'student_name': student_name,
            'student_id_num': student_id_num
        }
        log_activity(
            request.user, 
            'STUDENT_DELETED',
            log_details 
        )
        
        return JsonResponse({
            'success': True, 
            'message': f'✅ Student {student_name} has been permanently deleted.',
            'user_id': str(user_id) # Send back the ID for the JS
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
